"""市場盤勢（/盤勢，2026-10-17）的對外抓取：所有來源都經過這裡。

規則（PLAN §3、§19.2；審查 GP2 I3）：
- 只收 https、port 443、沒有帳密、主機在白名單內；不轉址（各來源的網址都已實測是最終網址，
  例 trumpstruth 用 www、SSGA 用不帶 /us/en/intermediary 的路徑）。
- 底層用 bot 既有的 `safe_fetch.fetch`：工作執行緒＋總時間截止（慢慢滴的回應也會被切掉）、
  `trust_env=False`（不吃環境 proxy、~/.netrc）、解壓後的位元組上限（gzip 炸彈也算）。
- 先看狀態碼、再看 Content-Type：429／403（news.google.com 另加 503）→ 該主機冷卻 2 小時；
  3xx → moved；其他非 200 → status。
- 請求前先在帳本預扣（間隔與每日上限），帳本由 job 用 SQLite 實作、測試用記憶體版。

錯誤一律丟 `FetchError(kind)`，只帶種類、不帶回應內容（outcome 只記列舉值）。
這個模組 import 時不做任何 I/O；`safe_fetch` 在第一次抓取時才 import。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional, Protocol
from urllib.parse import urlsplit

ALLOWED_HOSTS = frozenset(
    {
        # 美股
        "api.nasdaq.com",
        "www.ssga.com",
        "www.moneydj.com",
        "cdn-api.cboe.com",
        "home.treasury.gov",
        "www.cftc.gov",
        "www.finra.org",
        "www.federalreserve.gov",
        "query1.finance.yahoo.com",
        "query2.finance.yahoo.com",
        "api.coinbase.com",
        # 台股
        "www.twse.com.tw",
        "openapi.twse.com.tw",
        "openapi.taifex.com.tw",
        "api.finmindtrade.com",
        # 新聞、選舉、川普原文
        "news.google.com",
        "www.trumpstruth.org",
        "gamma-api.polymarket.com",
        "api.elections.kalshi.com",
    }
)

COOLDOWN_SECONDS = 7200.0
CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 10.0
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126 Safari/537.36"
)
_RATE_LIMIT_503_HOSTS = frozenset({"news.google.com"})


class FetchError(Exception):
    """kind：host、budget、cooldown、timeout、network、too_big、blocked_url、
    rate_limited（429）、blocked（403）、moved（3xx）、status、content_type、not_json。"""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str]
    url: str


class Ledger(Protocol):
    def reserve(self, host: str) -> None:
        """間隔或每日次數不夠、或主機在冷卻中就丟 FetchError("budget"／"cooldown")。"""

    def cooldown(self, host: str, seconds: float) -> None:
        """把主機放進冷卻（429／403 之後）。"""


def host_of(url: str) -> Optional[str]:
    """合格網址的主機名（小寫）；不是 https:443、帶帳密、或不在白名單就回 None。"""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        return None
    host = (parts.hostname or "").lower()
    return host if host in ALLOWED_HOSTS else None


def fetch(
    url: str,
    *,
    max_bytes: int,
    timeout: float = 15.0,
    accept: tuple[str, ...] = ("json",),
    headers: Optional[dict[str, str]] = None,
    ledger: Optional[Ledger] = None,
) -> Response:
    """抓一次；`timeout` 是整個請求的總時間上限（秒），`accept` 是 Content-Type 要含的子字串。"""
    host = host_of(url)
    if host is None:
        raise FetchError("host")
    if ledger is not None:
        ledger.reserve(host)
    import requests  # noqa: PLC0415 - 只有真的要抓時才載入
    import safe_fetch  # noqa: PLC0415

    merged = {"User-Agent": _UA, "Accept": "*/*"}
    merged.update(headers or {})
    try:
        resp = safe_fetch.fetch(
            url,
            headers=merged,
            timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
            deadline=timeout,
            max_bytes=max_bytes,
            accept_types=None,  # 先看狀態碼再看型別：429 的 HTML 頁也要能觸發冷卻
            allow_redirects=False,
        )
    except safe_fetch.TooLarge:
        raise FetchError("too_big") from None
    except safe_fetch.FetchTimeout:
        raise FetchError("timeout") from None
    except safe_fetch.FetchRefused:
        raise FetchError("blocked_url") from None
    except requests.Timeout:
        raise FetchError("timeout") from None
    except requests.RequestException:
        raise FetchError("network") from None
    except OSError:
        raise FetchError("network") from None
    status = int(resp.status_code)
    if status == 429 or (status == 503 and host in _RATE_LIMIT_503_HOSTS):
        if ledger is not None:
            ledger.cooldown(host, COOLDOWN_SECONDS)
        raise FetchError("rate_limited")
    if status == 403:
        if ledger is not None:
            ledger.cooldown(host, COOLDOWN_SECONDS)
        raise FetchError("blocked")
    if 300 <= status < 400:
        raise FetchError("moved")
    if status != 200:
        raise FetchError("status")
    content_type = str(resp.headers.get("Content-Type") or "").lower()
    if accept and not any(token in content_type for token in accept):
        raise FetchError("content_type")
    body = resp.content or b""
    return Response(status, body, {str(k).lower(): str(v) for k, v in resp.headers.items()}, url)


def fetch_json(url: str, **kwargs: Any) -> Any:
    """抓 JSON（容許 UTF-8 BOM）；解析不了丟 FetchError("not_json")。"""
    kwargs.setdefault("accept", ("json",))
    response = fetch(url, **kwargs)
    try:
        return json.loads(response.body.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise FetchError("not_json") from None


def fetch_text(url: str, *, encoding: str = "utf-8", **kwargs: Any) -> str:
    """抓文字（CSV、RSS、HTML）；解碼錯誤的字換成 U+FFFD，交給解析器判斷。"""
    response = fetch(url, **kwargs)
    return response.body.decode(encoding, errors="replace").lstrip("\ufeff")
