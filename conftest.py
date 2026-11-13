"""
pytest conftest — resets all mutable globals in main.py before/after every test.

Without this, global state leaks between tests:
  - _quota_exhausted_until_ts set by one test bleeds into the next
  - _grok_intro_sent_groups accumulates across tests (Bug 4 pattern)
  - _QUOTA_STATE_FILE writes touch the real quota_state.json (Bug 5 pattern)
  - pending_store.PENDING_PATH module-level constant writes touch the real
    pending_explicit_reply.json (2026-05-17 incident: pytest 寫穿 production
    pending → 10 筆使用者待回訊息被清空)
  - finance_view_db._DB_PATH module-level constant writes touch real line_bot.db
    (2026-05-18 加：跟 pending_store 同 pattern 隔離)
  - notify_discord.send_dm / _alert_quality_violation 真實打 Discord webhook
    (2026-05-19 事件：tests/test_chat_golden.py 用 NEWS_CASE keyword
    + 短 mock reply 觸發 _quality_gate retry 3 次仍違規 → 真實打 Discord DM
    騷擾使用者。fix = autouse fixture 全 test 攔住對外副作用)
"""

import os
import importlib
import pathlib
import sqlite3
import shutil
import tempfile
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

try:
    from pydantic.warnings import PydanticDeprecatedSince212

    warnings.filterwarnings(
        "ignore",
        message=(
            r"Using `@model_validator` with mode='after' on a classmethod "
            r"is deprecated\..*"
        ),
        category=PydanticDeprecatedSince212,
        module=r"google\.genai\.types",
    )
except Exception:
    pass

warnings.filterwarnings(
    "ignore",
    message=r"pkg_resources is deprecated as an API\..*",
    category=UserWarning,
    module=r"jieba\._compat",
)
warnings.filterwarnings(
    "ignore",
    message=r"builtin type SwigPy(?:Packed|Object) has no __module__ attribute",
    category=DeprecationWarning,
)
warnings.filterwarnings(
    "ignore",
    message=r"builtin type swigvarlink has no __module__ attribute",
    category=DeprecationWarning,
)

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("GROK_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")
# 2026-10-16 字卡大字版：測試預設不產生網頁、不加按鈕（本機有 tunnel 網址檔），要測的測試自己打開
# 2026-11-07 聊行情帶報價：預設關（不連 Yahoo），要測的測試自己打開。
os.environ["LINE_BOT_CHAT_QUOTES"] = "0"
os.environ["LINE_BOT_CARD_PAGES"] = "0"  # 直接設：shell 有 export 也不能讓測試讀真的 tunnel 檔、寫進 state/
# 2026-10-23 爸媽健康大字版（N7）：清健康頁不看上面的開關（刪除、暫停、收回訊息都要清），測試之間與
# 測試結束後的頁面目錄也一律指到隔離路徑，絕不落回正式 state/card_pages；每個測試另有自己的（isolate_card_pages）。
os.environ["LINE_BOT_CARD_PAGES_DIR"] = str(
    Path(tempfile.gettempdir()) / f"line_bot_pytest_quarantine_{os.getpid()}_card_pages"
)
os.environ.pop("LINE_BOT_EXERCISE_NUDGE", None)  # 2026-10-23 運動提醒：程式預設關，測試不吃 shell 的設定
# 2026-10-24 自動學習糾正：預設關（舊的糾正擷取照舊），要測的測試自己打開；狀態檔指到不存在的隔離路徑
# （絕不讀到正式 state/ 的開關檔）。判斷器的模型呼叫由下面的 isolate_complaint_learning 換成丟例外。
os.environ["LINE_BOT_COMPLAINT_LEARNING"] = "0"
# 2026-10-28 爸媽健康改推群組的開關：測試預設私訊。設成空字串（＝私訊）而不是刪掉：main、todo、weekly_summary
# 這類模組 import 時會 load_dotenv(override=False)，刪掉的話，測試中途才 import 的模組會把正式 .env 的開關加回來。
os.environ["LINE_BOT_PARENT_HEALTH_GROUP_PUSH"] = ""
# 2026-11-12 「提到人就真 @」的開關：測試照預設（開）。理由同上，設成空字串而不是刪掉；正式 .env 寫了 =0 也不影響測試。
os.environ["LINE_BOT_PUSH_MENTIONS"] = ""
# 2026-10-17 TikTok 讀取：預設關閉（0 = 不送 Gemini、不下載），要測的測試用假物件自己打開
# TODO(2026-10-17 D4): 這只關掉讀影片／圖片；沒有替換 yt-dlp 的既有測試仍可能真的連網抓 metadata。
os.environ["LINE_BOT_TIKTOK_MEDIA_DAILY_CAP"] = "0"
# 2026-11-06 保底以外的額外讀取：預設關，要測的測試自己打開。
os.environ["LINE_BOT_TIKTOK_MEDIA_QUIET_DAY_MAX"] = "0"
os.environ["LINE_BOT_YOUTUBE_MEDIA_DAILY_CAP"] = "0"  # 2026-11-07 同上
# Semantic restatement judge calls Gemini; unit tests enable it explicitly with a fake.
os.environ.setdefault("LINE_BOT_RESTATEMENT_JUDGE", "0")
# 2026-10-04: the separate-quota last Gemini tier stays off unless a test
# turns it on (many tests script exactly the two 2.5 models).
os.environ.setdefault("GEMINI_LAST_TIER_MODEL", "")
_ORIG_SQLITE_ENV = os.environ.get("SQLITE_PATH")
_POST_TEST_SQLITE_PATH = Path(tempfile.gettempdir()) / (
    f"line_bot_pytest_quarantine_{os.getpid()}.db"
)
os.environ["SQLITE_PATH"] = str(_POST_TEST_SQLITE_PATH)
# 2026-10-11 每日記憶：每個測試另有自己的路徑（isolate_daily_memory）；這裡是測試之間、
# 以及測試結束後還在跑的背景執行緒用的隔離路徑——絕不落回正式 state/ 的 ledger 或 HMAC 金鑰
# （金鑰指紋記在 DB 裡，隔離的 DB 要配隔離的金鑰）。
_POST_TEST_DAILY_MEMORY_DIR = Path(tempfile.gettempdir()) / (
    f"line_bot_pytest_quarantine_{os.getpid()}_daily_memory"
)
for _env_name, _leaf in (
    ("LINE_BOT_FACT_LEDGER_PATH", "fact_ledger.jsonl"),
    ("LINE_BOT_DAILY_MEMORY_STATE_DIR", "state"),
    ("LINE_BOT_PRIVACY_TERMS_PATH", "privacy_terms.txt"),
    ("LINE_BOT_FACT_LOCK_DIR", "locks"),
    ("LINE_BOT_FACT_HASH_KEY_PATH", "fact_hash.key"),
    ("LINE_FAMILY_KINSHIP_PATH", "no_kinship.json"),  # 2026-10-17：不存在的路徑，絕不讀到正式的角色檔
    ("LINE_BOT_COMPLAINT_LEARNING_SWITCH", "no_complaint_learning_switch.txt"),  # 2026-10-24：不存在＝不限制
):
    os.environ[_env_name] = str(_POST_TEST_DAILY_MEMORY_DIR / _leaf)

# PySpark needs to know the venv Python (system Python default breaks Java workers)
import sys as _sys
os.environ.setdefault("PYSPARK_PYTHON", _sys.executable)
os.environ.setdefault("PYSPARK_DRIVER_PYTHON", _sys.executable)

# 2026-11-13 測試全域網路閘門（block_network）。在 import main 之前就裝，而且故意永遠不拆；為什麼、擋什麼、擋不到什麼見
# block_network 的說明。計畫、探測、審查、部署在 ops/state/line_bot_test_network_gate_20261112/。
# 改這段之前：它要留在所有專案模組的 import 之前（新的 import 放在 block_network() 下面）；換掉的函式有增減時，
# test_network_gate.py 寫死的 10 個名字和 ops/state/…/mutants.py 的逐字片段要一起改；改任何一行都要重審。
import _socket  # noqa: E402
import errno  # noqa: E402
import ipaddress  # noqa: E402
import socket  # noqa: E402
import weakref  # noqa: E402

_NETWORK_GATE_MESSAGE = "tests may not use the network (conftest.py block_network)"
# 正式 bot 的埠（preflight_check.UVICORN_PORT，test_network_gate.py 釘住兩邊一致）：就算這個行程自己綁了同一號也不放行。
# 模組層變數：test_network_gate.py 用 monkeypatch 換成自己開的埠來測，絕不真的去連 8080。
_GATE_NEVER_OURS = frozenset({8080})
# 這個 pytest 行程裡 bind 過的 TCP／UDP socket：連本機時只放行同種類、同家族、還開著的那一個的埠。
_gate_bound_sockets: "weakref.WeakSet[socket.socket]" = weakref.WeakSet()
_gate_bound_lock = threading.Lock()
# 故意永遠不 undo：整輪結束後還沒跑完的背景工作（ThreadPoolExecutor 佇列裡的、atexit）也要擋。
_gate_monkeypatch = pytest.MonkeyPatch()
_gate_installed = False
# 裝閘門前的原版（test_network_gate.py 用它檢查沒有模組在閘門裝好前就存了一份原版）。
_GATE_ORIGINALS: dict = {}


def _gate_refusal(what) -> str:
    return f"{_NETWORK_GATE_MESSAGE}: {what}"


def _gate_host(host) -> str:
    if isinstance(host, (bytes, bytearray)):
        return bytes(host).decode("ascii", "replace")
    return str(host)


def _gate_numeric_addresses(host) -> "list[str] | None":
    """libc 只讀字串（AI_NUMERICHOST，絕不問 DNS）讀得懂就是數字位址，回它代表的位址；讀不懂（是名字）回 None。
    127.1、0x7f.1、2130706433 這類舊式寫法 ipaddress 讀不懂，libc 讀得懂。用 C 的 _socket.getaddrinfo，
    不能改成 socket.getaddrinfo：那個已經是閘門版，會繞回自己。"""
    try:
        infos = _socket.getaddrinfo(host, None, 0, 0, 0, socket.AI_NUMERICHOST)
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    return [info[4][0] for info in infos]


def _gate_ip(address: str):
    return ipaddress.ip_address(address.split("%", 1)[0])


def _gate_addresses_are_this_machine(addresses) -> bool:
    # Python 3.13 起，IPv4-mapped（::ffff:127.0.0.1）的 is_loopback／is_unspecified 就看 IPv4 那一半。
    return bool(addresses) and all(_gate_ip(a).is_loopback or _gate_ip(a).is_unspecified for a in addresses)


def _gate_is_localhost_name(host) -> bool:
    return _gate_host(host).lower() in ("localhost", "localhost.")


def _gate_is_this_machine(host) -> bool:
    """這台機器自己：確切的 "localhost"（不分大小寫，交給系統的 hosts 檔），或系統只讀字串（不問 DNS）就讀成
    127.0.0.0/8、::1（帶 scope 也算）、IPv4-mapped 的 127.x、萬用位址 0.0.0.0／::（連過去就是連本機）的寫法。
    這台電腦自己的主機名稱算外面。不先拿掉 % 或方括號：系統拿到的是原字串，"127.0.0.1%x.invalid"、"[::1]"
    這類寫法它會當名字去查 DNS，所以不算本機。"""
    return _gate_is_localhost_name(host) or _gate_addresses_are_this_machine(_gate_numeric_addresses(host))


def _gate_family_on_the_wire(caller, host) -> int:
    """IPv6 socket 連 IPv4 位址（127.0.0.1，或 IPv4-mapped 的 ::ffff:127.0.0.1）時，封包實際走 IPv4，
    接的是 IPv4 的聽口。"""
    if caller.family == socket.AF_INET6:
        ips = [_gate_ip(a) for a in _gate_numeric_addresses(host) or []]
        if ips and all(ip.version == 4 or ip.ipv4_mapped is not None for ip in ips):
            return socket.AF_INET
    return caller.family


def _gate_port_is_ours(kind, family, port) -> bool:
    """要連的是這個 port、種類 kind（TCP／UDP）、封包實際走 family（IPv4／IPv6）。TCP 和 UDP、IPv4 和 IPv6
    各有自己的埠：同號碼不代表是同一個 server，要種類、家族都對得上；IPv4 只認綁在 :: 而且收 IPv4
    （沒設 IPV6_V6ONLY）的雙棧 IPv6。"""
    if port in _GATE_NEVER_OURS:
        return False
    with _gate_bound_lock:
        candidates = list(_gate_bound_sockets)
    for own in candidates:
        try:
            if own.fileno() == -1 or own.type != kind:
                continue
            bound_host, bound_port = own.getsockname()[:2]
            if bound_port != port:
                continue
            if own.family == family:
                return True
            if (
                own.family == socket.AF_INET6
                and family == socket.AF_INET
                and bound_host == "::"
                and not own.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY)
            ):
                return True
        except OSError:  # closed in between
            continue
    return False


def _gate_remember(sock) -> None:
    """記下「這個行程的」socket：bind 過的，以及送出過資料、connect 過的 UDP socket（系統替它挑了埠，回覆就寄到那裡）。"""
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        with _gate_bound_lock:
            _gate_bound_sockets.add(sock)


def _gate_remember_datagram(sock) -> None:
    """UDP 要在真的送出（或 connect）之前就記：對方可能在這個呼叫回來前就回覆。還沒拿到埠的 socket 埠號是 0，
    不會對上任何人。"""
    if sock.type == socket.SOCK_DGRAM:
        _gate_remember(sock)


def _gate_check_address(caller, address) -> None:
    """connect／connect_ex／sendto／sendmsg 的目的地。AF_UNIX 的路徑（不是 tuple）放行，見 block_network。"""
    if not isinstance(address, tuple) or len(address) < 2:
        return
    host, port = address[0], address[1]
    if _gate_is_this_machine(host):
        if _gate_port_is_ours(caller.type, _gate_family_on_the_wire(caller, host), port):
            return
        raise ConnectionRefusedError(
            errno.ECONNREFUSED, _gate_refusal(f"loopback port {port} was not opened by this test run")
        )
    raise OSError(_gate_refusal(f"{_gate_host(host)}:{port}"))


def _gate_refuse_lookup(host, error=socket.gaierror, code=socket.EAI_NONAME) -> None:
    if host and not _gate_is_this_machine(host):
        raise error(code, _gate_refusal(f"no lookup of {_gate_host(host)}"))


def _install_network_gate(mp: pytest.MonkeyPatch) -> None:
    real = {
        "connect": socket.socket.connect,
        "connect_ex": socket.socket.connect_ex,
        "sendto": socket.socket.sendto,
        "sendmsg": socket.socket.sendmsg,
        "bind": socket.socket.bind,
        "getaddrinfo": socket.getaddrinfo,
        "gethostbyname": socket.gethostbyname,
        "gethostbyname_ex": socket.gethostbyname_ex,
        "gethostbyaddr": socket.gethostbyaddr,
        "getnameinfo": socket.getnameinfo,
    }
    _GATE_ORIGINALS.update(real)

    def connect(sock, address, *args, **kwargs):
        _gate_check_address(sock, address)
        _gate_remember_datagram(sock)
        return real["connect"](sock, address, *args, **kwargs)

    def connect_ex(sock, address, *args, **kwargs):
        try:
            _gate_check_address(sock, address)
        except ConnectionRefusedError:  # 本機別人的埠：跟沒人在聽一樣回 ECONNREFUSED；對外照樣丟例外
            return errno.ECONNREFUSED
        _gate_remember_datagram(sock)
        return real["connect_ex"](sock, address, *args, **kwargs)

    def sendto(sock, data, *args):  # sendto(data, address) / sendto(data, flags, address)
        if args:
            _gate_check_address(sock, args[-1])
        _gate_remember_datagram(sock)
        return real["sendto"](sock, data, *args)

    def sendmsg(sock, *args):  # sendmsg(buffers[, ancdata[, flags[, address]]])
        if len(args) >= 4:
            _gate_check_address(sock, args[3])
        _gate_remember_datagram(sock)
        return real["sendmsg"](sock, *args)

    def bind(sock, address, *args, **kwargs):
        result = real["bind"](sock, address, *args, **kwargs)
        _gate_remember(sock)
        return result

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if host and not _gate_is_localhost_name(host):
            addresses = _gate_numeric_addresses(host)
            if addresses is None:  # 是名字：不查
                raise socket.gaierror(socket.EAI_NONAME, _gate_refusal(f"no lookup of {_gate_host(host)}"))
            if not _gate_addresses_are_this_machine(addresses):
                flags |= socket.AI_NUMERICHOST  # 別台的數字位址：只在本機讀字串，絕不問 DNS；之後的 connect 照樣由閘門管
        return real["getaddrinfo"](host, port, family, type, proto, flags)

    def gethostbyname(host):
        _gate_refuse_lookup(host)
        return real["gethostbyname"](host)

    def gethostbyname_ex(host):
        _gate_refuse_lookup(host)
        return real["gethostbyname_ex"](host)

    def gethostbyaddr(host):
        _gate_refuse_lookup(host, socket.herror, 1)  # 1 = HOST_NOT_FOUND，跟真的反查失敗一樣
        return real["gethostbyaddr"](host)

    def getnameinfo(sockaddr, flags):
        if isinstance(sockaddr, tuple) and sockaddr:
            _gate_refuse_lookup(sockaddr[0])
        return real["getnameinfo"](sockaddr, flags)

    for target, name, gate in (
        (socket.socket, "connect", connect),
        (socket.socket, "connect_ex", connect_ex),
        (socket.socket, "sendto", sendto),
        (socket.socket, "sendmsg", sendmsg),
        (socket.socket, "bind", bind),
        (socket, "getaddrinfo", getaddrinfo),
        (socket, "gethostbyname", gethostbyname),
        (socket, "gethostbyname_ex", gethostbyname_ex),
        (socket, "gethostbyaddr", gethostbyaddr),
        (socket, "getnameinfo", getnameinfo),
    ):
        gate.network_gate = True  # test_network_gate.py 用這個記號確認裝的是閘門版
        mp.setattr(target, name, gate)

    # yt-dlp 偽裝瀏覽器時走 curl_cffi：libcurl 在 C 裡自己查 DNS、自己連線，上面的 socket 擋不到，在 Python 入口擋。
    # 丟 curl_cffi 自己的 CurlError（6 = CURLE_COULDNT_RESOLVE_HOST，跟真的斷網一樣）：呼叫端只接 CurlError，
    # 丟別的會在串流執行緒裡被吞掉，只剩「HTTP Error 0」。
    try:
        from curl_cffi.curl import Curl, CurlError
    except ImportError:  # 沒裝：yt-dlp 只會走 Python 的 HTTP，上面已經擋住
        return

    def perform(curl, *args, **kwargs):
        raise CurlError(_gate_refusal("curl_cffi"), 6)

    perform.network_gate = True
    mp.setattr(Curl, "perform", perform)


def block_network() -> None:
    """測試不能連到這台機器以外，也不能連到這台機器上別的程式：正式 bot 就在 127.0.0.1:8080，它的 /jobs 信任本機。

    - 什麼時候裝：conftest 一被 import 就裝，在它 import main 之前，所以 main 和它帶進來的模組存下來的也是閘門版。
      故意永遠不拆：整輪結束後 ThreadPoolExecutor 佇列裡還沒跑的工作、atexit 都還會跑。同一個行程只裝一次。
    - 擋什麼：
      - 對外的 connect／connect_ex／sendto／sendmsg 丟 OSError。
      - 查名字：getaddrinfo、gethostbyname、gethostbyname_ex、getnameinfo 查不是本機的名字丟 gaierror；
        gethostbyaddr 丟 herror。本機的名字只有 localhost，這台電腦自己的主機名稱算外面。
      - 數字位址：getaddrinfo 照常在本機讀出來、不問 DNS，連過去才擋；別台的數字位址強制加 AI_NUMERICHOST，
        所以 family=AF_INET6 查 IPv4 字面位址會丟 gaierror（真的斷網時會給 ::ffff: 對映，這點比斷網嚴）。
        gethostbyname、gethostbyname_ex、getnameinfo、gethostbyaddr 連數字位址也擋，比真的斷網嚴，是故意的。
      - curl_cffi（libcurl）的 Curl.perform 丟 CurlError（代碼 6，跟查不到主機一樣）。
    - 本機（_gate_is_this_machine）：只放行這個 pytest 行程自己的 socket 的埠，也就是 bind 過的，或送出過資料、
      connect 過的 UDP（系統替它挑了埠，回覆寄到那裡）；而且要還開著、TCP／UDP 相同、家族照封包實際走的相同
      （IPv6 socket 連 127.0.0.1 或 ::ffff:127.x 算 IPv4；綁在 :: 又沒設 IPV6_V6ONLY 的 IPv6 也接 IPv4）。別的本機埠：
      TCP 丟 ConnectionRefusedError（connect_ex 回 ECONNREFUSED），跟沒人在聽一樣；UDP 在 connect 或送出時就丟。
      _GATE_NEVER_OURS（正式 bot 的 8080）就算自己綁了也一律拒絕。
    - 例外：AF_UNIX（socket 檔）不管，本機程式的 socket 檔（例如 postgres 的 /tmp/.s.PGSQL.5432）連得到。
    - 要測連線行為：在測試裡自己開本機 server，或換假物件；不要放寬這個閘門，也不要加開關。開 server 用 port 0：
      用指定號碼綁 0.0.0.0／:: 又設 SO_REUSEADDR 時，別人在 127.0.0.1 的同號聽口會接走連線，閘門看不出來。
      不要包 TLS：包了會換掉 socket，閘門就不認了。test_model_all_mention.py 自己的 _no_network 疊在上面，沒有衝突。
    - 閘門的錯走的是呼叫端「連不上」的那條路，不一定跟有網路時一樣：例如 Gemini 把它當暫時性錯誤，重試、多睡幾秒。
      背景執行緒沒接住閘門的錯時，警告（PytestUnhandledThreadExceptionWarning，或物件回收時才冒出的
      PytestUnraisableExceptionWarning）會掛在後面另一條測試上。
    - 擋不到（寫在這裡，別以為有）：
      - 子行程：Claude CLI 靠 run_gate_suite.sh 的 CLAUDE_CODE_CMD=/nonexistent/claude，手動跑 pytest 沒有這層；
        curl、ffmpeg、multiprocessing spawn 出來的行程等都沒有。
      - 自己在 C 裡連線的函式庫：libpq、uvloop／libuv 事件迴圈、curl_cffi 的 AsyncSession。
      - 繞過 Python 那層直接用 C 的：_socket、socket.SocketType、_socket.socket.connect(s, …)、
        importlib.reload(socket)（會把原版放回去）。
      - conftest 被 import 之前就跑的東西（pytest 外掛載入時）；不經 pytest、直接 `python test_x.py` 跑的檔。
      - 本機位址的反查（gethostbyaddr("127.0.0.1") 等）照常交給系統解析器。
      - 刻意偽造的型別（__str__ 騙人的字串、永遠相等的 port 物件）。整數 host 會丟 OSError，不是原本的 TypeError。
    """
    global _gate_installed
    if _gate_installed:
        return
    _install_network_gate(_gate_monkeypatch)
    _gate_installed = True


block_network()

import burst_filter  # noqa: E402
import claude_client  # noqa: E402
import config  # noqa: E402
import finance_view_db  # noqa: E402
import gemini_client  # noqa: E402
import main  # noqa: E402
import media_pipeline  # noqa: E402
import tiktok_media  # noqa: E402
import memory  # noqa: E402
import pending_store  # noqa: E402

# Snapshot originals once at import time (before any test runs)
_ORIG_QUOTA_STATE_FILE = main._QUOTA_STATE_FILE
_ORIG_PENDING_PATH = main._PENDING_EXPLICIT_PATH
_ORIG_PENDING_STORE_PATH = pending_store.PENDING_PATH
_ORIG_PENDING_STORE_LOCK = pending_store.LOCK_PATH
_ORIG_BOT_MUTED = main.settings.bot_muted
_ORIG_PENDING_REPLY_ENABLED = main._PENDING_REPLY_ENABLED
_ORIG_ALLOWED_GROUP_ID = main.settings.allowed_group_id
# 2026-05-27 multi-group: tests 用 main.settings.allowed_group_id = "GRP001" 設 gate target，
# 但 webhook gate 讀 settings.allowed_group_ids（computed_field）。當 raw 從 env 載入非空時，
# computed 走 raw 分支忽略 singular → gate 不認 test group → silent drop。
# Fix: 每個 test setup 把 allowed_group_ids_raw 暫時清空，computed 走 singular fallback。
_ORIG_ALLOWED_GROUP_IDS_RAW = main.settings.allowed_group_ids_raw
_ORIG_FV_DB_PATH = finance_view_db._DB_PATH
_ORIG_MEMORY_DB_PATH = memory._DB_PATH
_ORIG_GEMINI_USAGE_FILE = gemini_client._USAGE_FILE
_ORIG_GEMINI_MODEL_USAGE_FILE = gemini_client._MODEL_USAGE_FILE
_ORIG_GEMINI_MODEL_USAGE_LOCK_FILE = gemini_client._MODEL_USAGE_LOCK_FILE

_SQLITE_MODULE_NAMES = (
    "memory",
    "calendar_db",
    "embedding_recall",
    "todo",
    "finance_view_db",
    "food_db",
    "family_poll",
    "anniversary",
    "knowledge_graph",
    "message_classifier",
)
# 2026-10-11 爸媽健康：這個模組本身不存在時（還沒部署）跳過，不讓整套測試在 setup 就失敗；
# 它 import 別的東西失敗時照樣報錯，不會默默寫到錯的 DB。
_OPTIONAL_SQLITE_MODULE_NAMES = ("parent_health_db",)


def _sqlite_sidecar_paths(db_path: Path) -> tuple[str, str, str]:
    return (str(db_path), f"{db_path}-wal", f"{db_path}-shm")


def _configure_sqlite_module(module_name: str, db_path: Path) -> None:
    module = importlib.import_module(module_name)
    if hasattr(module, "_DB_PATH"):
        module._DB_PATH = db_path

    if module_name == "memory":
        init = module._init_db
    elif module_name == "knowledge_graph":
        if not hasattr(module, "_ensure_table"):
            return
        init = module._ensure_table
    elif module_name == "message_classifier":
        if not hasattr(module, "ensure_schema"):
            return
        module._SCHEMA_ENSURED = False
        if hasattr(module, "_SCHEMA_ENSURED_PATHS"):
            module._SCHEMA_ENSURED_PATHS.clear()
        init = lambda: module.ensure_schema(db_path)
    elif hasattr(module, "init_db"):
        init = module.init_db
    else:
        return

    for attempt in range(8):
        try:
            init()
            return
        except sqlite3.OperationalError as e:
            if "database is locked" not in str(e).lower() or attempt == 7:
                raise
            time.sleep(0.05 * (attempt + 1))


def _configure_test_sqlite(db_path: Path) -> None:
    os.environ["SQLITE_PATH"] = str(db_path)
    config.settings.sqlite_path = str(db_path)
    main.settings.sqlite_path = str(db_path)
    for module_name in _SQLITE_MODULE_NAMES:
        _configure_sqlite_module(module_name, db_path)
    for module_name in _OPTIONAL_SQLITE_MODULE_NAMES:
        try:
            _configure_sqlite_module(module_name, db_path)
        except ModuleNotFoundError as exc:
            if exc.name != module_name:
                raise


def _restore_sqlite_paths() -> None:
    """After a test, never point async-capable modules back at production DB.

    Background daemon threads/executors may still run after fixture teardown.
    Keeping module globals on a quarantine DB makes future missed async writers
    fail safe instead of writing through to the real line_bot.db.
    """
    _configure_test_sqlite(_POST_TEST_SQLITE_PATH)


@pytest.fixture(autouse=True)
def reset_main_globals(monkeypatch):
    """Reset all mutable globals before AND after each test."""
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    tmp_quota_path = tmp.name
    tmp_usage = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp_usage.write(b'{"date":"pytest","tokens":0,"requests":0}')
    tmp_usage.close()
    tmp_model_usage = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp_model_usage.write(b"{}")
    tmp_model_usage.close()

    tmp_pending = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp_pending.write(b"{}")
    tmp_pending.close()
    tmp_pending_path = pathlib.Path(tmp_pending.name)
    tmp_pending_lock = pathlib.Path(tmp_pending.name + ".lock")

    tmp_app_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_app_db.close()
    tmp_app_db_path = Path(tmp_app_db.name)

    # 2026-10-10: display names the bot looks up (line_mentions) go to a temp
    # file, never state/member_display_names.json of the tree running the tests.
    # monkeypatch restores it after any test's own setenv of the same variable.
    tmp_display_names = Path(tempfile.mkdtemp()) / "member_display_names.json"
    monkeypatch.setenv("LINE_MEMBER_DISPLAY_NAMES_PATH", str(tmp_display_names))
    # 2026-10-10: the family's real user_aliases.json / family_roles.local.json
    # must not leak into tests that expect no @ ping on the receipt (爸爸 媽媽
    # resolve in the real file). A fixed test alias map keeps real family
    # names unlisted while giving tests a resolvable alias so helpers that
    # key off "有 user alias 設定" (e.g. aliases_mentioned_in_text) still work
    # for "測試成員甲"; tests that need their own aliases setenv
    # their own path to override.
    import json as _json
    tmp_alias_dir = Path(tempfile.mkdtemp())
    tmp_alias_path = tmp_alias_dir / "user_aliases.json"
    tmp_alias_path.write_text(
        _json.dumps(
            {"U_TEST_A": "測試成員甲", "U_TEST_B": "測試成員乙"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tmp_role_path = tmp_alias_dir / "family_roles.local.json"
    tmp_role_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(tmp_alias_path))
    monkeypatch.setenv("LINE_FAMILY_ROLE_ALIASES_PATH", str(tmp_role_path))
    main._member_labels.clear()
    with burst_filter._labelled_lock:
        burst_filter._labelled_texts.clear()

    # ── before ────────────────────────────────────────────────────────────
    # 2026-09-27: an 8 s burst timer left by an earlier test (e.g. an event run
    # through _handle_event) must not flush inside this one and hit its
    # patched _llm_chat / _reply.
    with burst_filter._lock:
        for timer in burst_filter._timers.values():
            timer.cancel()
        burst_filter._timers.clear()
        burst_filter._pending.clear()
        burst_filter._last_reply_tokens.clear()
        burst_filter._inflight.clear()
        burst_filter._inflight_batches.clear()
    # 2026-10-24: tests reuse reply-token constants; the token -> inbound lookup the send fence
    # keeps after a reply was accepted must not carry one test's event into the next.
    main._inbound_ids_by_token.clear()
    # 2026-10-24: a candidate nudge / mention target registered for a reply token by one test (and
    # not taken) must not wait for the next test that happens to use the same token.
    with main._exercise_nudge_lock:
        main._exercise_nudge_by_token.clear()
    with main._reply_mention_targets_lock:
        main._reply_mention_targets_by_token.clear()
    # 2026-10-28: the in-memory refusal counters (a rider refused without being named, a fenced
    # acknowledgement) must not carry one test's count into the next.
    main.reply_riders.reset_counters()
    # 2026-10-07: CLI timeouts faked by one test must not leave the CLI
    # resting (claude_client's timeout breaker) in the next.
    claude_client._cli_timeout_streak = 0
    claude_client._cli_timeout_breaker_until = 0.0
    # 2026-10-10: media_pipeline._OCR_ADMISSION is a BoundedSemaphore(1) and
    # _OCR_EXECUTOR is a single-worker ThreadPoolExecutor. Both leak state
    # across tests: (a) add_done_callback(release) fires on the executor
    # thread AFTER future.result() returns on the main thread, so a prior
    # test can leave the semaphore at 0 for a window; (b) future.cancel()
    # on a running task does not stop the thread, so a slow_ocr sleep
    # stays on the executor and blocks the next test's submit. Replace
    # both at test start; _extract_ocr_with_deadline captures sem locally
    # so in-flight callbacks safely release the stale instance.
    try:
        media_pipeline._OCR_EXECUTOR.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass
    media_pipeline._OCR_ADMISSION = threading.BoundedSemaphore(1)
    media_pipeline._OCR_EXECUTOR = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="media-ocr"
    )
    main._TIKTOK_MEDIA_SLOT = threading.BoundedSemaphore(1)
    main._TIKTOK_METADATA_SLOTS = threading.BoundedSemaphore(2)
    _configure_test_sqlite(tmp_app_db_path)
    main._quota_exhausted_until_ts = 0.0
    main._quota_notified_for_ts = 0.0
    main._quota_last_probe_ts = 0.0
    gemini_client._USAGE_FILE = tmp_usage.name
    gemini_client._MODEL_USAGE_FILE = tmp_model_usage.name
    gemini_client._MODEL_USAGE_LOCK_FILE = tmp_model_usage.name + ".lock"
    gemini_client._last_tier_backoff_until = 0.0
    gemini_client._side_task_backoff_until = 0.0
    gemini_client._media_backoff_until = 0.0
    gemini_client._media_bad_requests = 0
    gemini_client._media_cap_warned = ""
    gemini_client._media_quiet_day_max_warned = ""
    tiktok_media.clear_cache()
    main._PENDING_REPLY_ENABLED = _ORIG_PENDING_REPLY_ENABLED
    main._PENDING_EXPLICIT_PATH = str(tmp_pending_path)
    main._QUOTA_STATE_FILE = tmp_quota_path  # isolate file I/O
    pending_store.PENDING_PATH = tmp_pending_path  # 防 production pending 被寫穿
    pending_store.LOCK_PATH = tmp_pending_lock
    main.settings.bot_muted = True
    main.settings.allowed_group_id = _ORIG_ALLOWED_GROUP_ID
    main.settings.allowed_group_ids_raw = ""  # 清空讓 computed 走 singular fallback

    yield

    # ── after ─────────────────────────────────────────────────────────────
    claude_client._cli_timeout_streak = 0
    claude_client._cli_timeout_breaker_until = 0.0
    main._quota_exhausted_until_ts = 0.0
    main._quota_notified_for_ts = 0.0
    main._quota_last_probe_ts = 0.0
    gemini_client._USAGE_FILE = _ORIG_GEMINI_USAGE_FILE
    gemini_client._MODEL_USAGE_FILE = _ORIG_GEMINI_MODEL_USAGE_FILE
    gemini_client._MODEL_USAGE_LOCK_FILE = _ORIG_GEMINI_MODEL_USAGE_LOCK_FILE
    gemini_client._last_tier_backoff_until = 0.0
    gemini_client._side_task_backoff_until = 0.0
    gemini_client._media_backoff_until = 0.0
    gemini_client._media_bad_requests = 0
    gemini_client._media_cap_warned = ""
    gemini_client._media_quiet_day_max_warned = ""
    tiktok_media.clear_cache()
    main._PENDING_REPLY_ENABLED = _ORIG_PENDING_REPLY_ENABLED
    main._PENDING_EXPLICIT_PATH = _ORIG_PENDING_PATH
    main._QUOTA_STATE_FILE = _ORIG_QUOTA_STATE_FILE
    pending_store.PENDING_PATH = _ORIG_PENDING_STORE_PATH
    pending_store.LOCK_PATH = _ORIG_PENDING_STORE_LOCK
    _restore_sqlite_paths()
    main.settings.bot_muted = _ORIG_BOT_MUTED
    main.settings.allowed_group_id = _ORIG_ALLOWED_GROUP_ID
    main.settings.allowed_group_ids_raw = _ORIG_ALLOWED_GROUP_IDS_RAW
    main._member_labels.clear()
    with burst_filter._labelled_lock:
        burst_filter._labelled_texts.clear()
    # TODO(2026-11-06): an exception earlier in this teardown skips these two and the
    # folders stay.  Rare, and only the temp dir pays for it.
    shutil.rmtree(tmp_display_names.parent, ignore_errors=True)
    shutil.rmtree(tmp_alias_dir, ignore_errors=True)

    for p in (
        tmp_quota_path,
        tmp_usage.name,
        tmp_model_usage.name,
        tmp_model_usage.name + ".lock",
        tmp_pending.name,
        str(tmp_pending_lock),
        *_sqlite_sidecar_paths(tmp_app_db_path),
    ):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass


@pytest.fixture(autouse=True)
def block_external_side_effects(monkeypatch, request):
    """禁止 test 期間打真實 Discord DM / quality-violation alert。

    Root cause (2026-05-19): tests/test_chat_golden.py 用 NEWS_CASE keyword
    （「保險」「AI 詐騙新聞解析」）+ 短 mock reply（「這是個測試回覆。」）
    觸發 _quality_gate retry 3 次仍違規 → _alert_quality_violation 真實打
    Discord webhook，每次 pytest 至少 4~6 通 DM。

    在 conftest 一次擋住，比每個 test 自己 patch 穩；新 test 也自動受惠。
    """
    import notify_discord
    monkeypatch.setattr(notify_discord, "send_dm", lambda *a, **kw: True, raising=False)

    # 雙保險：直接攔 entry point，未來 production code 若新增其他 alert
    # 也不會在 test 偷打
    import gemini_client
    monkeypatch.setattr(
        gemini_client, "_alert_quality_violation",
        lambda *a, **kw: None, raising=False,
    )
    # 同步攔 _log_quality_violation — tests 在 prod line_bot.db 的 persona_notes
    # 留下大量 C_test_group 違規記錄（2026-05-23 auto iterate 發現 24h 內 30 筆
    # 全是 mock 回覆「這是個測試回覆。」），污染自我學習資料 + 拖慢人工排查。
    monkeypatch.setattr(
        gemini_client, "_log_quality_violation",
        lambda *a, **kw: None, raising=False,
    )
    if request.path.name != "test_embedding_recall.py":
        import embedding_recall
        monkeypatch.setattr(
            embedding_recall,
            "index_message",
            lambda *a, **kw: None,
            raising=False,
        )

    # 2026-09-27: nothing fetched through safe_fetch leaves the machine — no
    # lookup, and no address counts as public, so even a literal public IP is
    # refused before connecting.  Transport tests re-open their local server.
    import safe_fetch

    def _no_lookup(_host, _port):
        raise safe_fetch.BlockedURL("tests never resolve hostnames")

    monkeypatch.setattr(safe_fetch, "_resolve", _no_lookup)
    monkeypatch.setattr(safe_fetch, "is_public_ip", lambda _ip: False)

    # 2026-10-28 爸媽健康改推群組：LINE 推播一律換成只記錄的替身（不連網、不扣額度）。push_text、try_push_*
    # 用到時才查這個模組屬性，也會走到替身。呼叫端自己帶 session 的（event_reminder、測試給的假 session）照舊走
    # 真的：連線本來就由呼叫端決定，而且 event_reminder 用 from-import，第一次在測試裡 import 時綁到的是替身。
    # 直接測 push_messages 本身的傳輸層測試檔也照舊用真的。
    if request.path.name not in _REAL_LINE_PUSH_TEST_FILES:
        import line_push_client

        pushed: list[dict] = []
        real_push = line_push_client.push_messages

        def _record_push(to, messages, **kwargs):
            if kwargs.get("session") is not None:
                return real_push(to, messages, **kwargs)
            pushed.append({"to": to, "messages": list(messages), **kwargs})
            return True

        _record_push.calls = pushed
        monkeypatch.setattr(line_push_client, "push_messages", _record_push)
    # 改推群組的開關每個測試都從空字串（私訊）開始：不刪掉，測試中途 load_dotenv(override=False) 才不會把正式 .env
    # 的開關加回來。要測群組模式的測試自己 setenv。conftest 預設靜音（main.settings.bot_muted = True），
    # 要看到推播的測試也要自己解除靜音。
    monkeypatch.setenv("LINE_BOT_PARENT_HEALTH_GROUP_PUSH", "")
    monkeypatch.setenv("LINE_BOT_PUSH_MENTIONS", "")  # 「提到人就真 @」照預設（開）


# 直接測 line_push_client.push_messages 的傳輸層測試（自己給假的 session，不能被上面的替身換掉）
_REAL_LINE_PUSH_TEST_FILES = ("test_line_push_client.py", "test_output_validator.py")


_DAILY_MEMORY_ENV_KNOBS = (
    "LINE_BOT_DAILY_MEMORY",
    "LINE_BOT_DAILY_MEMORY_BACKEND",
    "LINE_BOT_DAILY_MEMORY_MODEL",
    "LINE_BOT_DAILY_MEMORY_HEARSAY",
    "LINE_BOT_DAILY_MEMORY_HEARSAY_ONLY",
    "LINE_BOT_DAILY_MEMORY_FALLBACK",
    "LINE_BOT_FACTS_PER_PERSON_MAX",
    "LINE_BOT_INLINE_FACT_EXTRACT",
    "LINE_BOT_FACT_DEDUPE_HIGH",
    "LINE_BOT_FACT_DEDUPE_LOW",
)


@pytest.fixture(autouse=True)
def isolate_daily_memory(monkeypatch, tmp_path_factory):
    """2026-10-11 每日記憶：ledger、job 狀態、隱私詞清單（空檔）、鎖、HMAC 金鑰一律指到
    這個測試自己的暫存目錄——正式 state/fact_ledger.jsonl 不能多出測試的列（回滾會照它刪），
    也不讀本機真實的隱私詞。fact_store 預設不用嵌入模型（要用的測試自己注入假模型，
    不載入 MiniLM）。開關與門檻不吃部署環境（.env 或 job 的 load_dotenv）帶進來的值。"""
    base = tmp_path_factory.mktemp("daily_memory")
    terms = base / "privacy_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setenv("LINE_BOT_FACT_LEDGER_PATH", str(base / "fact_ledger.jsonl"))
    monkeypatch.setenv("LINE_BOT_DAILY_MEMORY_STATE_DIR", str(base / "state"))
    monkeypatch.setenv("LINE_BOT_PRIVACY_TERMS_PATH", str(terms))
    monkeypatch.setenv("LINE_BOT_FACT_LOCK_DIR", str(base / "locks"))
    monkeypatch.setenv("LINE_BOT_FACT_HASH_KEY_PATH", str(base / "fact_hash.key"))
    monkeypatch.setenv("LINE_FAMILY_KINSHIP_PATH", str(base / "no_kinship.json"))
    for name in _DAILY_MEMORY_ENV_KNOBS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config.settings, "facts_per_person_max_raw", "", raising=False)
    monkeypatch.setattr(config.settings, "inline_fact_extract_raw", "", raising=False)
    try:
        import fact_store
    except Exception:  # fact_store 自己壞了：只讓它的測試失敗
        return
    monkeypatch.setattr(fact_store, "loaded_embedder", lambda: None)


@pytest.fixture(autouse=True)
def isolate_parent_health(monkeypatch):
    """2026-10-11：爸媽健康的背景工作（抽取 executor、每日檢查執行緒）不能在測試裡起來；要測抽取就
    直接呼叫 observe_message。模組不存在或換成緊急停用版時也不影響其他測試。"""
    try:
        import parent_health
    except Exception:
        return
    monkeypatch.setattr(parent_health, "_BACKGROUND_ENABLED", False, raising=False)


@pytest.fixture(autouse=True)
def isolate_card_pages(monkeypatch, tmp_path_factory):
    """2026-10-23（GP2 N7）：大字版網頁目錄一律指到這個測試自己的暫存目錄。清健康頁不看開關，
    刪除、暫停、收回訊息的測試會呼叫它：在正式目錄跑 pytest（每日維護、部署前）也不能刪到正式的頁。
    運動提醒的開關每個測試都從「沒設」開始（程式預設關）。"""
    monkeypatch.setenv("LINE_BOT_CARD_PAGES_DIR", str(tmp_path_factory.mktemp("card_pages") / "pages"))
    monkeypatch.delenv("LINE_BOT_EXERCISE_NUDGE", raising=False)
    monkeypatch.delenv("LINE_BOT_CARD_PAGE_HEALTH", raising=False)
    monkeypatch.delenv("LINE_BOT_CARD_PAGE_HEALTH_TTL_HOURS", raising=False)


@pytest.fixture(autouse=True)
def isolate_complaint_learning(monkeypatch, tmp_path_factory):
    """2026-10-24 自動學習糾正：背景 worker 不在測試裡起來（要測就直接呼叫 process_due）；判斷器與驗證的
    Claude CLI／Gemini 呼叫一律丟例外（要測的測試自己換成假的）；開關預設關、狀態檔指到這個測試自己的
    不存在路徑；佇列、待判與每則留言的事件紀錄每個測試清空。"""
    monkeypatch.setenv("LINE_BOT_COMPLAINT_LEARNING", "0")
    monkeypatch.setenv(
        "LINE_BOT_COMPLAINT_LEARNING_SWITCH",
        str(tmp_path_factory.mktemp("complaint_learning") / "complaint_learning_switch.txt"),
    )
    try:
        import complaint_learning
    except Exception:  # 模組自己壞了：只讓它的測試失敗
        yield
        return

    def _no_model(*_args, **_kwargs):
        raise RuntimeError("tests never call the complaint judge model")

    monkeypatch.setattr(complaint_learning, "_BACKGROUND_ENABLED", False, raising=False)
    monkeypatch.setattr(complaint_learning, "_claude_json", _no_model, raising=False)
    monkeypatch.setattr(complaint_learning, "_gemini_json", _no_model, raising=False)
    complaint_learning.reset_for_tests()
    yield
    complaint_learning.reset_for_tests()


@pytest.fixture(autouse=True)
def isolate_stock_picks(monkeypatch, tmp_path_factory):
    """2026-10-10：股票推薦的背景執行緒不能在測試裡起來（有測試會跑 _app_lifespan）；
    狀態檔一律指到這個測試自己的暫存目錄、不從磁碟載入，不碰正式 state/；stock_picks 的對外
    請求一律擋掉（需要資料的測試自己換 `_get_json`）；單檔查詢的限流與快取每個測試清掉。"""
    try:
        import stock_picks
    except Exception:  # stock_picks 自己壞了：只讓它的測試失敗，不連帶整套測試在 setup 就失敗
        return

    monkeypatch.delenv("LINE_BOT_STOCK_PICKS_AI", raising=False)
    monkeypatch.setattr(stock_picks, "AI_ENABLED", True, raising=False)
    monkeypatch.setattr(stock_picks, "_AI_BACKGROUND_ENABLED", False, raising=False)
    monkeypatch.setattr(stock_picks, "_BACKGROUND_ENABLED", False, raising=False)
    if not hasattr(stock_picks, "WorkerState"):  # 緊急停用版（stock_picks_disabled.py）沒有這些
        return

    def _no_network(*_args, **_kwargs):
        raise AssertionError("stock_picks must not touch the network in tests")

    monkeypatch.setattr(stock_picks, "_get_json", _no_network)
    monkeypatch.setattr(stock_picks, "STATE_DIR", tmp_path_factory.mktemp("stock_picks_state"))
    monkeypatch.setattr(stock_picks, "_snapshot", None)
    monkeypatch.setattr(stock_picks, "_worker", stock_picks.WorkerState())
    monkeypatch.setattr(stock_picks, "_loaded", True)
    # AI 段的記憶體狀態每個測試都從全新的開始（dict 每次新建，測試之間不共用；2026-10-24 計畫 C）。
    for name, fresh in (
        ("_ai_state", None), ("_ai_next_try", 0.0), ("_ai_cache_t", None), ("_ai_chart_cache", {}),
        ("_ai_holdings", None), ("_ai_fetch_budget", {}), ("_ai_hole_first", {}), ("_ai_fail_tries", {}),
        ("_ai_skip_check", {"n": 0, "next": 0.0}), ("_yahoo_blocked_until", 0.0),
    ):
        monkeypatch.setattr(stock_picks, name, fresh, raising=False)
    # 狀態檢查會讀 .env 裡的 LINE_BOT_STOCK_PICKS_AI：測試一律指到不存在的檔，不讀真的 .env。
    monkeypatch.setattr(stock_picks, "ENV_FILE", tmp_path_factory.mktemp("stock_picks_env") / ".env", raising=False)
    stock_picks.clear_caches()
    # 美股與推薦成績（2026-10-28）：開關與 DB 路徑不讀真的環境（DB 預設在上面的暫存 STATE_DIR 裡，不存在）；
    # 記憶體狀態、快取、限流、冷卻每個測試都從全新的開始。
    monkeypatch.delenv("LINE_BOT_STOCK_PICKS_US", raising=False)
    monkeypatch.delenv("LINE_BOT_MARKET_DB_PATH", raising=False)
    try:
        import stock_picks_us

        stock_picks_us.reset_for_tests()
    except Exception:  # 美股模組自己壞了（或還沒有）：只讓它的測試失敗，不連帶整套測試在 setup 就失敗
        pass


@pytest.fixture(autouse=True)
def isolate_market_dashboard(monkeypatch, tmp_path_factory):
    """2026-10-17：市場盤勢的快照、旗標、歷史庫、圖目錄一律指到這個測試自己的暫存目錄，不碰正式
    state/ 與 /tmp/line_bot_genimg。對外連線另外由上面的 safe_fetch 守門擋掉（market_http 建在它上面）。"""
    try:
        import market_paths
    except Exception:  # market_paths 自己壞了：只讓它的測試失敗
        return
    root = tmp_path_factory.mktemp("market")
    (root / "state").mkdir(mode=0o700)
    paths = market_paths.Paths.under(root)
    monkeypatch.setattr(market_paths.Paths, "default", classmethod(lambda cls: paths))


@pytest.fixture(autouse=True)
def isolate_stock_picks_review(monkeypatch, tmp_path_factory):
    """2026-10-29 股票推薦回測 job（jobs/stock_picks_review.py）：bot 目錄（state/、盤勢 DB、推薦狀態檔）與
    outbox 所在的維護目錄一律指到這個測試自己的暫存目錄。job 的路徑都在呼叫時才從這兩個環境變數算，
    所以任何測試跑到 job 的程式都不會碰正式 state/，也不會把週報或短訊放進正式 outbox（relay 會立刻 DM Andrew）。"""
    root = tmp_path_factory.mktemp("stock_picks_review")
    (root / "bot" / "state").mkdir(parents=True)
    (root / "bot" / "state").chmod(0o700)  # 暫存目錄是測試自己的：job 要求 state/ 是 0700
    (root / "maintenance").mkdir()
    monkeypatch.setenv("STOCK_PICKS_REVIEW_BOT_DIR", str(root / "bot"))
    monkeypatch.setenv("STOCK_PICKS_REVIEW_MAINTENANCE_DIR", str(root / "maintenance"))


# 2026-11-12 自動學習糾正週報（jobs/learned_rules_weekly.py）：outbox 所在的維護目錄、DB、狀態目錄、DB 私人副本的
# 上層目錄一律指到不存在的隔離路徑，而且不建目錄（審查 GP2 G1：每個測試多一個 numbered mktemp 要掃整個 basetemp）。
# job 的路徑都在呼叫時才從這四個環境變數算；路徑不存在時它不抄 DB、不放檔（outbox 所在目錄不在就 exit 2，DB 不在就
# db_missing），所以任何測試跑到 job 都不會讀正式 DB，也不會把週報放進正式 outbox（relay 會立刻 DM Andrew）。
# 要測的測試（tests/test_learned_rules_weekly.py）自己在 tmp_path 建目錄、用 monkeypatch 指過去。
_LEARNED_RULES_WEEKLY_ABSENT = Path(tempfile.gettempdir()) / (
    f"line_bot_pytest_quarantine_{os.getpid()}_learned_rules_weekly_absent"
)
for _env_name, _leaf in (
    ("LEARNED_RULES_WEEKLY_MAINTENANCE_DIR", "maintenance"),
    ("LEARNED_RULES_WEEKLY_DB", "line_bot.db"),
    ("LEARNED_RULES_WEEKLY_STATE_DIR", "state"),
    ("LEARNED_RULES_WEEKLY_COPY_PARENT", "tmp"),
):
    os.environ[_env_name] = str(_LEARNED_RULES_WEEKLY_ABSENT / _leaf)
