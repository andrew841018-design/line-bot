"""2026-11-13 測試全域網路閘門（conftest.py 的 block_network）：擋得住什麼、放行什麼。

這個檔本身不連網。每條會呼叫網路函式的測試，先確認那個函式是閘門版（_gated，看 network_gate 記號）；不是就直接
失敗，什麼都不送。被擋的目的地另外也只用保留給文件與測試的（192.0.2.0/24、.invalid 網域）；本機的 server 和聽口
都是測試自己開在 127.0.0.1／::1 的，絕不碰正式 bot 的 8080（那條用 monkeypatch 換成自己的埠）；萬用位址只 bind、
不 listen。
"""

import _socket
import errno
import os
import shutil
import socket
import socketserver
import sys
import tempfile
import threading

import pytest

import conftest
import preflight_check
import safe_fetch

GATE = "tests may not use the network"
OFF_MACHINE = "192.0.2.1"  # TEST-NET-1：文件專用，不會有真的主機
NO_SUCH_NAME = "netgate-probe.invalid"  # .invalid 永遠查不到


def _is_gate_version(function) -> bool:
    return getattr(function, "network_gate", False) is True


def _gated(*functions):
    for function in functions:
        assert _is_gate_version(function), f"{function!r} is not the network gate's version"


def _lookup_outcome() -> str:
    if not _is_gate_version(socket.getaddrinfo):
        return "not gated"  # 閘門沒裝：不真的去查
    try:
        socket.getaddrinfo(NO_SUCH_NAME, 443)
    except OSError as exc:
        return str(exc)
    return "resolved"


# 收集這個檔（import）時就查一次：閘門在 conftest 被 import 時就裝好，收集階段也要擋。
_LOOKUP_WHILE_COLLECTING = _lookup_outcome()


class _Echo(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.sendall(self.request.recv(16))


def _serve(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def _foreign_listener(port, family=_socket.AF_INET, host="127.0.0.1"):
    """用 C 的 socket 型別開的聽口：閘門的 bind 看不到它，就像別的行程。綁不到（號碼被占）回 None。"""
    other = _socket.socket(family, _socket.SOCK_STREAM)
    try:
        other.bind((host, port))
        other.listen(1)
    except OSError:
        other.close()
        return None
    return other


def _foreign_listeners_on_both_loopbacks():
    """同一個號碼在 127.0.0.1 和 ::1 都開別人的聽口："localhost" 兩個位址都會試，閘門壞掉時也只連到這兩個。"""
    for _attempt in range(20):
        v4 = _foreign_listener(0)
        assert v4 is not None
        v6 = _foreign_listener(v4.getsockname()[1], _socket.AF_INET6, "::1")
        if v6 is not None:
            return v4, v6
        v4.close()
    pytest.skip("no free port pair")


def test_the_gate_is_up_while_test_files_are_collected():
    assert GATE in _LOOKUP_WHILE_COLLECTING


def test_the_gate_is_installed_once():
    installed = socket.socket.connect
    assert _is_gate_version(installed) and conftest._gate_installed is True
    conftest.block_network()  # 再叫一次不會再包一層
    assert socket.socket.connect is installed


def test_no_module_kept_an_ungated_copy_of_a_network_function():
    # 閘門在 conftest import main 之前就裝：之後才 import 的模組存下來的都是閘門版。只檢查，不改寫。
    originals = conftest._GATE_ORIGINALS
    assert set(originals) == {
        "connect", "connect_ex", "sendto", "sendmsg", "bind",
        "getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo",
    }
    original_ids = {id(function) for function in originals.values()}
    kept = []
    for module_name, module in list(sys.modules.items()):
        if module is None or module_name in ("socket", "_socket"):
            continue
        try:
            attributes = list(vars(module).items())
        except TypeError:
            continue
        kept += [f"{module_name}.{name}" for name, value in attributes if id(value) in original_ids]
    assert kept == []


def test_a_lookup_function_saved_at_import_time_is_gated():
    # safe_fetch 在 import 時就存了 socket.getaddrinfo（conftest import main 時載入）。
    _gated(safe_fetch._connect_getaddrinfo)
    with pytest.raises(OSError, match=GATE):
        safe_fetch._connect_getaddrinfo(NO_SUCH_NAME, 443)


@pytest.mark.parametrize(
    "host, local",
    [
        ("127.0.0.1", True),
        ("127.255.0.9", True),
        ("::1", True),
        ("::1%lo0", True),
        ("[::1]", False),  # 系統不把方括號當數字位址，會拿去查 DNS
        ("127.0.0.1%x.invalid", False),  # 同上：% 後面接的東西也會讓它變成名字
        ("[localhost]", False),
        ("localhost%x", False),
        ("::ffff:127.0.0.1", True),
        ("localhost", True),
        ("localhost.", True),
        ("LOCALHOST", True),
        (b"localhost", True),
        (b"127.0.0.1", True),
        ("127.1", True),  # 舊式寫法：libc 只讀字串就讀成 127.0.0.1
        ("0x7f.1", True),
        ("2130706433", True),
        ("0.0.0.0", True),  # 萬用位址：連過去就是連本機
        ("::", True),
        (OFF_MACHINE, False),
        ("10.0.0.1", False),
        ("192.168.1.1", False),
        ("::ffff:192.0.2.1", False),
        ("2001:db8::1", False),
        (NO_SUCH_NAME, False),
        ("api.line.me", False),
        ("", False),
    ],
)
def test_what_counts_as_this_machine(host, local):
    assert conftest._gate_is_this_machine(host) is local  # 純函式：不開 socket、不問 DNS


@pytest.mark.parametrize("kind", [socket.SOCK_STREAM, socket.SOCK_DGRAM], ids=["tcp", "udp"])
def test_a_connect_off_the_machine_is_refused(kind):
    # UDP 的 connect 不送封包，但之後的 send() 不經閘門：connect 這一步就要擋。
    _gated(socket.socket.connect)
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.settimeout(1)
        with pytest.raises(OSError, match=GATE):
            sock.connect((OFF_MACHINE, 9))


@pytest.mark.parametrize("host", [OFF_MACHINE.encode(), bytearray(OFF_MACHINE.encode())], ids=["bytes", "bytearray"])
def test_a_bytes_host_is_checked_like_a_string(host):
    _gated(socket.socket.connect)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        with pytest.raises(OSError, match=GATE):
            sock.connect((host, 80))


@pytest.mark.parametrize("call", ["connect", "connect_ex"])
def test_a_tcp_connect_off_the_machine_is_refused(call):
    _gated(getattr(socket.socket, call))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        with pytest.raises(OSError, match=GATE):
            getattr(sock, call)((OFF_MACHINE, 80))


def test_connect_ex_to_someone_elses_local_port_says_refused():
    _gated(socket.socket.connect_ex)
    other = _foreign_listener(0)
    assert other is not None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            assert sock.connect_ex(("127.0.0.1", other.getsockname()[1])) == errno.ECONNREFUSED
    finally:
        other.close()


@pytest.mark.parametrize(
    "name, lookup",
    [
        ("getaddrinfo", lambda: socket.getaddrinfo(NO_SUCH_NAME, 443)),
        ("gethostbyname", lambda: socket.gethostbyname(NO_SUCH_NAME)),
        ("gethostbyname", lambda: socket.gethostbyname(OFF_MACHINE)),  # 數字位址也擋（比真的斷網嚴，故意的）
        ("gethostbyname_ex", lambda: socket.gethostbyname_ex(NO_SUCH_NAME)),
        ("gethostbyname_ex", lambda: socket.gethostbyname_ex(OFF_MACHINE)),  # 數字位址也擋
        ("getnameinfo", lambda: socket.getnameinfo((OFF_MACHINE, 443), 0)),
        ("getaddrinfo", lambda: socket.create_connection((NO_SUCH_NAME, 443), timeout=1)),
    ],
    ids=[
        "getaddrinfo", "gethostbyname", "gethostbyname-numeric", "gethostbyname_ex", "gethostbyname_ex-numeric",
        "getnameinfo", "create_connection",
    ],
)
def test_name_lookups_off_the_machine_are_refused(name, lookup):
    _gated(getattr(socket, name))
    with pytest.raises(OSError, match=GATE):
        lookup()


def test_a_refused_lookup_says_not_found_not_try_again():
    # EAI_AGAIN 會讓呼叫端當成暫時錯誤重試、多睡幾秒；查不到就是 EAI_NONAME。
    _gated(socket.getaddrinfo)
    with pytest.raises(socket.gaierror, match=GATE) as caught:
        socket.getaddrinfo(NO_SUCH_NAME, 443)
    assert caught.value.errno == socket.EAI_NONAME


def test_the_numeric_check_never_asks_the_resolver(monkeypatch):
    # 判斷「是不是本機」時只讓 libc 讀字串：每一次都要帶 AI_NUMERICHOST，名字絕不送進解析器。
    flags_seen = []
    me = threading.get_ident()

    class _NumericOnly:
        @staticmethod
        def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            if not flags & socket.AI_NUMERICHOST:
                raise AssertionError(f"{host!r} would have gone to the resolver")
            if threading.get_ident() == me:  # 別的執行緒同時在判斷也沒關係：只數這條測試自己的
                flags_seen.append(flags)
            return _socket.getaddrinfo(host, port, family, type, proto, flags)

    monkeypatch.setattr(conftest, "_socket", _NumericOnly())
    for host in ("api.line.me", NO_SUCH_NAME, OFF_MACHINE, "127.1", b"netgate-probe.invalid", "::1"):
        conftest._gate_is_this_machine(host)
    assert len(flags_seen) == 6 and all(flags & socket.AI_NUMERICHOST for flags in flags_seen)


def test_a_reverse_lookup_fails_like_a_real_one():
    _gated(socket.gethostbyaddr)
    with pytest.raises(socket.herror, match=GATE) as caught:
        socket.gethostbyaddr(OFF_MACHINE)
    assert caught.value.errno == 1  # HOST_NOT_FOUND


@pytest.mark.parametrize(
    "literal, address",
    [(OFF_MACHINE, OFF_MACHINE), ("127.1", "127.0.0.1"), ("0x7f.1", "127.0.0.1"), ("2130706433", "127.0.0.1")],
)
def test_a_numeric_address_is_read_here_without_a_lookup(literal, address):
    # 真的斷網時數字位址照樣解析得出來（不用 DNS），閘門也一樣；連過去才擋。
    _gated(socket.getaddrinfo)
    assert {info[4][0] for info in socket.getaddrinfo(literal, 80, type=socket.SOCK_STREAM)} == {address}


def test_a_numeric_address_off_the_machine_still_cannot_be_reached():
    _gated(socket.getaddrinfo, socket.socket.connect)
    with pytest.raises(OSError, match=GATE):
        socket.create_connection((OFF_MACHINE, 80), timeout=1)


def test_a_datagram_off_the_machine_is_refused():
    _gated(socket.socket.sendto, socket.socket.sendmsg)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(OSError, match=GATE):
            sock.sendto(b"x", (OFF_MACHINE, 9))
        with pytest.raises(OSError, match=GATE):
            sock.sendto(b"x", 0, (OFF_MACHINE, 9))
        with pytest.raises(OSError, match=GATE):
            sock.sendmsg([b"x"], [], 0, (OFF_MACHINE, 9))


@pytest.mark.parametrize("how", ["sendto", "sendmsg", "connect", "connect_ex"])
def test_a_udp_reply_to_a_client_that_never_bound_arrives(how):
    # client 沒 bind：系統在它第一次送出（或 connect）時才挑埠。server 回覆寄到那個埠，閘門也要認得它是自己人。
    _gated(socket.socket.sendto, socket.socket.sendmsg, socket.socket.connect, socket.socket.connect_ex, socket.socket.bind)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server, socket.socket(
        socket.AF_INET, socket.SOCK_DGRAM
    ) as client:
        server.bind(("127.0.0.1", 0))
        server.settimeout(5)
        client.settimeout(5)
        if how == "sendto":
            client.sendto(b"ping", server.getsockname())
        elif how == "sendmsg":
            client.sendmsg([b"ping"], [], 0, server.getsockname())
        elif how == "connect":
            client.connect(server.getsockname())
            client.send(b"ping")
        else:
            assert client.connect_ex(server.getsockname()) == 0
            client.send(b"ping")
        data, sender = server.recvfrom(16)
        server.sendto(b"pong", sender)
        assert data == b"ping" and client.recvfrom(16)[0] == b"pong"


def test_only_this_runs_bound_sockets_and_udp_senders_count_as_ours():
    _gated(socket.socket.connect, socket.socket.sendto, socket.socket.bind)
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Echo) as server:
        _serve(server)
        try:
            with socket.create_connection(server.server_address, timeout=5) as tcp_client:
                assert tcp_client not in set(conftest._gate_bound_sockets)  # 連出去的 TCP client 不算自己的聽口
        finally:
            server.shutdown()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as refused:
        with pytest.raises(OSError, match=GATE):
            refused.sendto(b"x", (OFF_MACHINE, 9))
        assert refused not in set(conftest._gate_bound_sockets)  # 被閘門擋下的不算


def test_a_numeric_address_off_the_machine_never_reaches_the_resolver(monkeypatch):
    # 別台的數字位址一定帶 AI_NUMERICHOST 送進 libc：就算判斷錯了，也只在本機讀字串、不問 DNS。
    _gated(socket.getaddrinfo)
    real = _socket.getaddrinfo
    me = threading.get_ident()

    def numeric_only(host, port, family=0, type=0, proto=0, flags=0):
        if threading.get_ident() == me and host == OFF_MACHINE:  # 只管這條測試自己查的那一個：別的執行緒照常
            assert flags & socket.AI_NUMERICHOST, f"{host!r} would have gone to the resolver"
        return real(host, port, family, type, proto, flags)

    monkeypatch.setattr(_socket, "getaddrinfo", numeric_only)
    assert socket.getaddrinfo(OFF_MACHINE, 80, type=socket.SOCK_STREAM)


def test_a_datagram_to_our_own_socket_arrives():
    _gated(socket.socket.sendto, socket.socket.bind)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server, socket.socket(
        socket.AF_INET, socket.SOCK_DGRAM
    ) as client:
        server.bind(("127.0.0.1", 0))
        server.settimeout(5)
        client.sendto(b"hi", server.getsockname())
        assert server.recvfrom(16)[0] == b"hi"


def test_loopback_names_still_resolve():
    _gated(socket.getaddrinfo, socket.gethostbyname, socket.getnameinfo)
    assert socket.getaddrinfo("localhost", 80, type=socket.SOCK_STREAM)
    assert socket.getaddrinfo(None, 80, flags=socket.AI_PASSIVE)  # 要 bind 的伺服器這樣查
    assert socket.gethostbyname("localhost").startswith("127.")
    assert socket.getnameinfo(("127.0.0.1", 80), socket.NI_NUMERICHOST | socket.NI_NUMERICSERV) == ("127.0.0.1", "80")


def test_unix_domain_sockets_are_left_alone():
    # AF_UNIX（socket 檔）不在閘門範圍：位址不是 (host, port)。路徑放在短的暫存資料夾（macOS 限 104 字元）。
    _gated(socket.socket.connect)
    folder = tempfile.mkdtemp(prefix="ng", dir="/tmp")
    path = os.path.join(folder, "s")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(path)
            server.listen(1)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(path)
                conn, _ = server.accept()
                conn.close()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_a_server_this_run_opened_is_reachable(host):
    _gated(socket.socket.connect, socket.socket.bind)
    for _attempt in range(20):
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Echo)
        # "localhost" 先試 ::1：同號也開別人的聽口佔住，閘門壞掉時也只連到這個佔位的。
        placeholder = _foreign_listener(server.server_address[1], _socket.AF_INET6, "::1") if host == "localhost" else None
        if host != "localhost" or placeholder is not None:
            break
        server.server_close()
    else:
        pytest.skip("no free port pair")
    _serve(server)
    try:
        with socket.create_connection((host, server.server_address[1]), timeout=5) as client:
            client.sendall(b"ping")
            assert client.recv(16) == b"ping"
    finally:
        server.shutdown()
        server.server_close()
        if placeholder is not None:
            placeholder.close()


def test_a_server_in_a_background_thread_can_be_reached_from_another_thread():
    _gated(socket.socket.connect, socket.socket.bind)
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Echo) as server:
        _serve(server)
        replies = []

        def client():
            with socket.create_connection(server.server_address, timeout=5) as conn:
                conn.sendall(b"pong")
                replies.append(conn.recv(16))

        try:
            worker = threading.Thread(target=client)
            worker.start()
            worker.join(10)
        finally:
            server.shutdown()
        assert replies == [b"pong"]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_a_loopback_port_this_run_did_not_open_is_refused(host):
    # 用 C 的 socket 型別開的聽口，閘門的 bind 看不到，就像別的行程（正式 bot 在 127.0.0.1:8080）。
    _gated(socket.socket.connect)
    v4, v6 = _foreign_listeners_on_both_loopbacks()
    try:
        with pytest.raises(ConnectionRefusedError, match=GATE):
            socket.create_connection((host, v4.getsockname()[1]), timeout=1)
    finally:
        v4.close()
        v6.close()


def test_an_ipv6_connect_to_someone_elses_local_port_is_refused():
    _gated(socket.socket.connect)
    other = _foreign_listener(0, _socket.AF_INET6, "::1")
    assert other is not None
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            with pytest.raises(ConnectionRefusedError, match=GATE):
                sock.connect(("::1", other.getsockname()[1], 0, 0))
    finally:
        other.close()


@pytest.mark.parametrize("address", ["2001:db8::1", "::ffff:192.0.2.1"])  # 文件專用的 IPv6，和走 IPv6 路的 TEST-NET-1
def test_an_ipv6_address_off_the_machine_is_refused(address):
    _gated(socket.socket.connect)
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        with pytest.raises(OSError, match=GATE):
            sock.connect((address, 80, 0, 0))


@pytest.mark.parametrize(
    "host, wire",
    [
        ("127.0.0.1", socket.AF_INET),  # IPv6 socket 連 IPv4 位址：系統對映成 ::ffff:127.0.0.1，走 IPv4
        ("::ffff:127.0.0.1", socket.AF_INET),
        ("::1", socket.AF_INET6),
        ("localhost", socket.AF_INET6),  # 名字：系統照 socket 的家族解析
    ],
)
def test_the_family_on_the_wire_for_an_ipv6_socket(host, wire):
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as caller:
        assert conftest._gate_family_on_the_wire(caller, host) == wire  # 純函式：不連線


@pytest.mark.parametrize("target", ["::ffff:127.0.0.1", "127.0.0.1"])
def test_an_ipv6_socket_to_an_ipv4_loopback_meets_ipv4_listeners(target):
    # ::ffff:127.0.0.1 實際走 IPv4：我們綁在 ::1 的同號碼 socket 不算數，接的會是別人的 IPv4 聽口。
    _gated(socket.socket.connect, socket.socket.bind)
    for _attempt in range(20):
        ours = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        ours.bind(("::1", 0))
        other = _foreign_listener(ours.getsockname()[1])
        if other is not None:
            break
        ours.close()
    else:
        pytest.skip("no free port pair")
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            with pytest.raises(ConnectionRefusedError, match=GATE):
                sock.connect((target, other.getsockname()[1], 0, 0))
    finally:
        other.close()
        ours.close()


def test_our_ipv4_server_does_not_open_someone_elses_ipv6_port():
    _gated(socket.socket.connect, socket.socket.bind)
    for _attempt in range(20):
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Echo)
        other = _foreign_listener(server.server_address[1], _socket.AF_INET6, "::1")
        if other is not None:
            break
        server.server_close()
    else:
        pytest.skip("no free port pair")
    other.setblocking(False)
    _serve(server)
    try:
        port = server.server_address[1]
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            with pytest.raises(ConnectionRefusedError, match=GATE):
                sock.connect(("::1", port, 0, 0))
        with socket.create_connection(("localhost", port), timeout=5) as client:  # 先試 ::1 被拒，再到自己的 IPv4
            client.sendall(b"ping")
            assert client.recv(16) == b"ping"
        with pytest.raises(BlockingIOError):
            other._accept()  # 別人的聽口沒收到任何連線（C 的型別只有 _accept）
    finally:
        server.shutdown()
        server.server_close()
        other.close()


@pytest.mark.parametrize("how", ["sendto", "sendmsg", "connect", "connect_ex"])
def test_a_udp_client_is_ours_before_its_first_datagram(monkeypatch, how):
    # 一定要在系統替 client 挑埠（真的送出或 connect）之前就記：記的那一刻，client 的埠號還是 0。
    _gated(socket.socket.sendto, socket.socket.sendmsg, socket.socket.connect, socket.socket.connect_ex, socket.socket.bind)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server, socket.socket(
        socket.AF_INET, socket.SOCK_DGRAM
    ) as client:
        server.bind(("127.0.0.1", 0))
        ports_when_remembered = []
        remember = conftest._gate_remember

        def spy(sock):
            if sock is client:
                ports_when_remembered.append(sock.getsockname()[1])
            remember(sock)

        monkeypatch.setattr(conftest, "_gate_remember", spy)
        target = server.getsockname()
        {
            "sendto": lambda: client.sendto(b"x", target),
            "sendmsg": lambda: client.sendmsg([b"x"], [], 0, target),
            "connect": lambda: client.connect(target),
            "connect_ex": lambda: client.connect_ex(target),
        }[how]()
        assert ports_when_remembered[:1] == [0]


def test_a_quick_udp_reply_from_another_thread_is_never_refused():
    # 先記再送：server 在 client 的 sendto 回來之前就回覆，也不能被當成別人的埠。
    _gated(socket.socket.sendto, socket.socket.bind)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        server.bind(("127.0.0.1", 0))
        server.settimeout(5)
        errors = []

        def echo(rounds):
            for _ in range(rounds):
                try:
                    data, sender = server.recvfrom(16)
                    server.sendto(data, sender)
                except OSError as exc:
                    errors.append(str(exc))
                    return

        worker = threading.Thread(target=echo, args=(200,))
        worker.start()
        try:
            for _ in range(200):
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                    client.settimeout(5)
                    client.sendto(b"x", server.getsockname())
                    if errors:
                        break
                    assert client.recvfrom(16)[0] == b"x"
        finally:
            worker.join(10)
        assert errors == []


def test_a_port_this_run_closed_is_not_ours_any_more():
    _gated(socket.socket.connect, socket.socket.bind)
    with socketserver.TCPServer(("127.0.0.1", 0), _Echo) as server:
        port = server.server_address[1]
    with pytest.raises(ConnectionRefusedError, match=GATE):
        socket.create_connection(("127.0.0.1", port), timeout=1)


@pytest.mark.parametrize(
    "own_family, own_type, own_host",
    [
        (socket.AF_INET, socket.SOCK_DGRAM, "127.0.0.1"),  # 同號碼的 UDP socket
        (socket.AF_INET6, socket.SOCK_STREAM, "::1"),  # 同號碼、綁在 ::1 的 IPv6 TCP socket
    ],
    ids=["udp-same-number", "ipv6-same-number"],
)
def test_our_socket_on_the_same_number_does_not_open_someone_elses_tcp_port(own_family, own_type, own_host):
    if own_family == socket.AF_INET6 and not socket.has_ipv6:
        pytest.skip("no IPv6 on this machine")
    _gated(socket.socket.connect, socket.socket.bind)
    for _attempt in range(20):
        ours = socket.socket(own_family, own_type)
        ours.bind((own_host, 0))
        other = _foreign_listener(ours.getsockname()[1])
        if other is not None:
            break
        ours.close()
    else:
        pytest.skip("no free port pair")
    try:
        with pytest.raises(ConnectionRefusedError, match=GATE):
            socket.create_connection(("127.0.0.1", other.getsockname()[1]), timeout=1)
    finally:
        other.close()
        ours.close()


@pytest.mark.parametrize("v6only, counts", [(False, True), (True, False)], ids=["dual-stack", "ipv6-only"])
def test_an_ipv6_socket_on_the_wildcard_counts_for_ipv4_only_when_dual_stack(v6only, counts):
    if not socket.has_ipv6:
        pytest.skip("no IPv6 on this machine")
    _gated(socket.socket.bind)
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as own, socket.socket(
        socket.AF_INET, socket.SOCK_STREAM
    ) as caller:
        own.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, int(v6only))
        own.bind(("::", 0))  # 只 bind、不 listen：不對區網開埠
        assert conftest._gate_port_is_ours(caller.type, caller.family, own.getsockname()[1]) is counts


def test_the_never_ours_port_is_the_bots_port():
    assert conftest._GATE_NEVER_OURS == frozenset({preflight_check.UVICORN_PORT})


def test_the_bots_port_is_never_ours_even_when_we_bound_it(monkeypatch):
    _gated(socket.socket.connect, socket.socket.bind)
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Echo) as server:
        _serve(server)
        try:
            # 把「正式 bot 的埠」換成這個測試自己開的埠：閘門壞掉時，最多連到自己的 server，碰不到 8080。
            monkeypatch.setattr(conftest, "_GATE_NEVER_OURS", frozenset({server.server_address[1]}))
            with pytest.raises(ConnectionRefusedError, match=GATE):
                socket.create_connection(server.server_address, timeout=1)
        finally:
            server.shutdown()


def test_a_background_thread_is_blocked_too():
    _gated(socket.getaddrinfo)
    errors = []

    def lookup():
        try:
            socket.getaddrinfo(NO_SUCH_NAME, 443)
        except OSError as exc:
            errors.append(str(exc))

    worker = threading.Thread(target=lookup)
    worker.start()
    worker.join(10)
    assert len(errors) == 1 and GATE in errors[0]


def test_curl_cffi_cannot_connect():
    curl = pytest.importorskip("curl_cffi.curl")
    _gated(curl.Curl.perform)
    with pytest.raises(curl.CurlError, match=GATE) as caught:
        curl.Curl().perform()  # 沒設網址：閘門壞掉時 libcurl 自己報錯，也不會連網
    assert caught.value.code == 6  # 跟真的斷網一樣：CURLE_COULDNT_RESOLVE_HOST
