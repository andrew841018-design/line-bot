"""市場盤勢（/盤勢，2026-10-24）的川普壓力指數（TACO 指數）來源：MacroMargin（ocmacro.com）公開頁面內嵌的資料。

Andrew 2026-10-17 要卡上有「川普壓力指數」的數字和歷史位階（「川普壓力指數要有那個數字啊！也要有他相對于歷史的位階
（這樣我查知道目前是在哪個狀況，會不會TACO)」）。財經M平方（MM）的指數頁對程式一律回 403＋Cloudflare 驗證（不繞過、不登入），
2026-10-24 Andrew 改選 MacroMargin 公開的「川普壓力指數（TACO 指數）」。

來源（2026-10-23 實測，investigations/trump_pressure_20261023.md §4；保存的回應在 research_bodies/trump_diag/run1/）：
- https://ocmacro.com/robots.txt 明確 Allow /dashboard/trump（Disallow /api/、/data/、其他 /dashboard/）。只抓這一頁：
  不碰 /api/、/data/，也不碰頁面上的「下載源數據」。個人網站、找不到使用條款；方法和 MM 不同（來源自述「不是政策回撤概率」）。
- 頁面是 Next.js（App Router）：資料在 HTML 裡一連串 `<script>self.__next_f.push([1,"…"])</script>`，每個字串是一個 JSON 字串，
  解開後依序串起來就是 React flight 文字（一行一個 row：`<十六進位 id>:<JSON>`）。含 "methodologyVersion" 的那一行是
  `…{"data":{"methodologyVersion","index":{"value","asOfDate","change20d","status":{"key"},…},"history":[{"date","value",…},…],…}}…`
  （2026-10-22：9.0005、change20d −3.6、status.key "high"、394 筆日資料，2025-03-26～2026-10-22）。
  保存的回應裡整行在同一個 push；別的 render 可能把一行切成幾個 push，所以一律先全部串起來再切行。

解析（嚴格；來源是第三方，頁面一改版就該失敗，而不是印出錯的數字）：
- 每個 push 用 `json.JSONDecoder.raw_decode` 解（不用正則抓字串）；不是 `[1, "字串"]` 的（`push([0])` 之類）略過。
  NaN／Infinity 一律拒收。
- 含 "methodologyVersion" 的行 → 去掉行首的 row id 與冒號 → JSON → 遞迴找「同時有 index 與 history 的 dict」；同一行裡
  必須剛好一個。多行都解得出資料時內容必須完全相同（相同才算一份），不同 → ParseError("conflict")。
- 只取數字與日期：methodologyVersion（`[A-Za-z0-9._-]{1,40}`）、index.value（有限、|v|≤100）、index.asOfDate（嚴格 YYYY-MM-DD）、
  index.change20d（有限、|v|≤200，或沒有）、index.status.key（`[a-z_]{1,16}`，或沒有）、history[].date／value（日期嚴格遞增、
  ≥60 筆、每筆有限且 |v|≤100）。最後一筆的日期要等於 asOfDate、值和 index.value 差 ≤0.01，否則 ParseError("conflict")。
- index.change20d（有的話）要和自己的歷史對得上：|change20d − (index.value − 歷史倒數第 21 筆)| ≤ 0.1，否則 ParseError("conflict",
  "change")（2026-10-28 審查 gp2 N7：卡片「20 個交易日變化」直接印它；來源自己把它四捨五入到一位小數，所以正常的差不到 0.05）。
- 其他格式問題一律 ParseError("format", <detail>)；detail 只放程式自己的列舉字（只有英文字母與底線：框架的 log 只收
  [A-Za-z_]{1,24}）。**來源的任何文字（簡體標籤、方法說明、因子名稱、網址）一律不存、不印**：結果只有上面那幾個欄位，
  錯誤只有 kind＋detail。

每個來源兩個函式（INTERFACES §3）：純解析 `parse_taco`（bytes／str）與抓取 `fetch_taco`（組固定網址、呼叫 `http.fetch`
（預設 market_http，測試注入假的）、再解析）。不 import main、stock_picks、card_pages，也不 import 其他 market_sources／
market_pipeline 模組（一個模組壞掉不能拖垮別的任務）；market_http 到真的要抓時才載入；import 時不做任何 I/O。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

import market_calc
from market_calc import ParseError  # 全專案共用

KIB = 1024
MIB = 1024 * 1024

URL = "https://ocmacro.com/dashboard/trump"
HOST = "ocmacro.com"


@dataclass(frozen=True)
class Endpoint:
    """抓取設定與每日預算（起始值；job 另外受帳本 ocmacro.com 每台北日 8 次、間隔 10 秒的限制）。"""

    host: str
    accept: tuple[str, ...]  # Content-Type 要含的子字串
    max_bytes: int
    timeout: float  # 整個請求的總時間上限（秒）
    per_day: int  # 每個台北日最多幾次（market_pipeline_taco.MAX_TRIES_PER_DAY）


# 2026-10-23 實測頁面 2.5 MB（gzip 前）；上限 4 MiB
ENDPOINTS: dict[str, Endpoint] = {"taco": Endpoint(HOST, ("text/html",), 4 * MIB, 30.0, 6)}
MAX_BYTES = ENDPOINTS["taco"].max_bytes

# ── 嚴格解析的界線 ────────────────────────────────────────────────────────────
VALUE_MAX = market_calc.TACO_VALUE_MAX  # |指數值| ≤ 100（實際約 −5～+17）
CHANGE_MAX = market_calc.TACO_CHANGE_MAX
MIN_HISTORY = market_calc.TACO_MIN_DAYS  # 歷史至少 60 筆
MAX_HISTORY = 3000  # 約 12 年的交易日；任期到 2029 年初也只有 ~1000 筆
LAST_ROW_TOLERANCE = market_calc.TACO_LAST_ROW_TOLERANCE
CHANGE_DAYS = market_calc.TACO_CHANGE_DAYS
CHANGE_TOLERANCE = market_calc.TACO_CHANGE_TOLERANCE
MAX_PUSHES = 5000  # 一頁最多看幾個 push（實測 15 個）
MAX_NODES = 1_000_000  # 找資料時最多走過幾個 JSON 節點
MAX_DEPTH = 24
DATE_MIN, DATE_MAX = date(2020, 1, 1), date(2100, 12, 31)

_PUSH = "self.__next_f.push("
_MARKER = '"methodologyVersion"'
_ROW_RE = re.compile(r"[0-9a-fA-F]{1,8}:")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_METHOD_RE = re.compile(r"[A-Za-z0-9._-]{1,40}")
_STATUS_RE = re.compile(r"[a-z_]{1,16}")


def _reject_constant(_name: str) -> Any:
    raise ValueError("non-finite json constant")  # NaN／Infinity／-Infinity


_DECODER = json.JSONDecoder(parse_constant=_reject_constant)


@dataclass(frozen=True)
class TacoSeries:
    """解析結果：只有數字、日期與兩個通過白名單的短字串（methodologyVersion、status.key）。"""

    method: str  # methodologyVersion；market_calc.TACO_METHOD_V1 = 目前的方法
    value: float  # index.value
    as_of: date  # index.asOfDate（美股交易日）
    change20d: Optional[float]  # index.change20d（較 20 個交易日前的指數點數差）
    status_key: Optional[str]  # index.status.key，例 "high"
    history: tuple[tuple[date, float], ...]  # 嚴格遞增的 (日期, 值)，最後一筆＝as_of


# ── 解析 ──────────────────────────────────────────────────────────────────────


def _as_text(payload: Any) -> str:
    if isinstance(payload, (bytes, bytearray, memoryview)):
        raw = bytes(payload)
        if len(raw) > MAX_BYTES:
            raise ParseError("format", "body_size")
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ParseError("format", "encoding") from None
    if isinstance(payload, str):
        if len(payload) > MAX_BYTES:
            raise ParseError("format", "body_size")
        return payload
    raise ParseError("format", "body")


def _flight_text(page: str) -> str:
    """每個 `self.__next_f.push([1, "字串"])` 的字串依序串起來（React flight 文字）。

    push 的內容用 raw_decode 解（不是正則）；解不出來的略過（頁面上別處剛好出現這串字不該讓整頁失敗，缺的資料後面照樣
    會被抓到）；不是 `[1, 字串]` 的（`push([0])`、型別 2／3）不要。"""
    parts: list[str] = []
    total = 0
    attempts = 0
    pos = 0
    while True:
        at = page.find(_PUSH, pos)
        if at < 0:
            break
        attempts += 1
        if attempts > MAX_PUSHES:
            raise ParseError("format", "too_many_push")
        start = at + len(_PUSH)
        try:
            item, end = _DECODER.raw_decode(page, start)
        except (ValueError, RecursionError):
            pos = start
            continue
        pos = end
        if isinstance(item, list) and len(item) == 2 and type(item[0]) is int and item[0] == 1 and isinstance(item[1], str):
            total += len(item[1])
            if total > MAX_BYTES:
                raise ParseError("format", "body_size")
            parts.append(item[1])
    if not parts:
        raise ParseError("format", "no_push")
    return "".join(parts)


def _find_data(root: Any) -> list[dict]:
    """同時有 "index" 與 "history" 的 dict（不往它裡面再找）；走訪的節點與深度都有上限。"""
    found: list[dict] = []
    stack: list[tuple[Any, int]] = [(root, 0)]
    nodes = 0
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES:
            raise ParseError("format", "too_big_json")
        if isinstance(node, dict):
            if "index" in node and "history" in node:
                found.append(node)
            elif depth < MAX_DEPTH:
                stack.extend((item, depth + 1) for item in node.values())
        elif isinstance(node, list) and depth < MAX_DEPTH:
            stack.extend((item, depth + 1) for item in node)
    return found


def _number(value: Any, limit: float) -> Optional[float]:
    """有限的數字（不收 bool、字串），|v| ≤ limit；大整數也能比，不會 OverflowError。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if abs(value) > limit:
            return None
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _day(value: Any) -> Optional[date]:
    """嚴格的 YYYY-MM-DD（3.11 之後的 fromisoformat 也收 20261022 這種）。"""
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return None
    return day if DATE_MIN <= day <= DATE_MAX else None


def _status_key(status: Any) -> Optional[str]:
    if status is None:
        return None
    if not isinstance(status, dict):
        raise ParseError("format", "status")
    key = status.get("key")
    if key is None:
        return None
    if not isinstance(key, str) or not _STATUS_RE.fullmatch(key):
        raise ParseError("format", "status")
    return key


def _history(raw: Any) -> tuple[tuple[date, float], ...]:
    if not isinstance(raw, list) or not MIN_HISTORY <= len(raw) <= MAX_HISTORY:
        raise ParseError("format", "history_count")
    rows: list[tuple[date, float]] = []
    previous: Optional[date] = None
    for item in raw:
        day = _day(item.get("date")) if isinstance(item, dict) else None
        value = _number(item.get("value"), VALUE_MAX) if isinstance(item, dict) else None
        if day is None or value is None:
            raise ParseError("format", "history_row")
        if previous is not None and day <= previous:
            raise ParseError("format", "history_order")
        rows.append((day, value))
        previous = day
    return tuple(rows)


def _series(data: dict) -> TacoSeries:
    method = data.get("methodologyVersion")
    if not isinstance(method, str) or not _METHOD_RE.fullmatch(method):
        raise ParseError("format", "method")
    index = data.get("index")
    if not isinstance(index, dict):
        raise ParseError("format", "index")
    value = _number(index.get("value"), VALUE_MAX)
    if value is None:
        raise ParseError("format", "value")
    as_of = _day(index.get("asOfDate"))
    if as_of is None:
        raise ParseError("format", "as_of")
    change = index.get("change20d")
    if change is not None:
        change = _number(change, CHANGE_MAX)
        if change is None:
            raise ParseError("format", "change")
    status_key = _status_key(index.get("status"))
    history = _history(data.get("history"))
    last_day, last_value = history[-1]
    if last_day != as_of or abs(last_value - value) > LAST_ROW_TOLERANCE + 1e-9:
        raise ParseError("conflict", "last_row")
    if change is not None and len(history) > CHANGE_DAYS \
            and abs(change - (value - history[-1 - CHANGE_DAYS][1])) > CHANGE_TOLERANCE + 1e-9:
        raise ParseError("conflict", "change")  # 來源給的 20 日變化和自己的歷史對不上：整頁不收
    return TacoSeries(method, value, as_of, change, status_key, history)


def parse_taco(payload: Any) -> TacoSeries:
    """頁面（bytes 或 str）→ TacoSeries；任何不符合上面規則的都丟 ParseError（只有 kind 與列舉 detail）。"""
    flight = _flight_text(_as_text(payload))
    candidates = [line for line in flight.split("\n") if _MARKER in line]
    if not candidates:
        raise ParseError("format", "no_line")
    found: list[TacoSeries] = []
    problem = "no_data"
    for line in candidates:
        row = _ROW_RE.match(line)
        if row is None:
            problem = "no_row"
            continue
        try:
            tree = _DECODER.decode(line[row.end():])
        except (ValueError, RecursionError):
            problem = "json"
            continue
        datas = _find_data(tree)
        if not datas:
            problem = "no_data"
            continue
        if len(datas) > 1:
            raise ParseError("format", "dup_data")
        found.append(_series(datas[0]))
    if not found:
        raise ParseError("format", problem)
    if any(item != found[0] for item in found[1:]):
        raise ParseError("conflict", "dup_line")
    return found[0]


# ── 抓取 ──────────────────────────────────────────────────────────────────────


def _transport(http: Any) -> Any:
    """抓取層：預設 market_http（用到才 import，這個模組沒有它也能 import 與測試）。"""
    if http is not None:
        return http
    import market_http  # noqa: PLC0415

    return market_http


def enabled() -> bool:
    """一個開關（照 BLS 的 bls_enabled）：ocmacro.com 在 market_http.ALLOWED_HOSTS 才抓；要停用只要把主機拿掉。每次呼叫才讀白名單
    （測試 monkeypatch）；market_http 到這裡才載入。"""
    return HOST in _transport(None).ALLOWED_HOSTS


def fetch_taco(*, http: Any = None, ledger: Any = None) -> TacoSeries:
    """抓 https://ocmacro.com/dashboard/trump（固定網址）並解析。http／ledger 由 job 的 Context 傳進來。"""
    endpoint = ENDPOINTS["taco"]
    response = _transport(http).fetch(
        URL, max_bytes=endpoint.max_bytes, timeout=endpoint.timeout, accept=endpoint.accept, ledger=ledger
    )
    body = getattr(response, "body", None)
    if not isinstance(body, (bytes, bytearray)):
        raise ParseError("format", "body")
    if len(body) > endpoint.max_bytes:
        raise ParseError("format", "body_size")  # 傳輸層已限制；假的或換掉的傳輸層也不能塞超大內容
    return parse_taco(bytes(body))
