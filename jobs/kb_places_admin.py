"""kb_places_admin — 地點知識庫唯一的寫入口（2026-10-31，第一批段一）。

Andrew 10/31：「全部照推薦（含 D8 排程寫 state/kb_places.json 的例外），開工段一」。
知識庫查證排程（line-bot-kb-places-verify，Andrew 10/31 選 PLAN.md 第 13 節選項 1）只能透過這支寫
``state/kb_places*``，不准用 Edit 手改；它的守門程式（ops/kb_verify/guard.py）也只放行這支的 propose、confirm、todo、stats。
設計與審查見 ops/state/line_bot_knowledge_base_20261030/PLAN.md（v2）與 reviews/。

兩輪查證（D3）：
- ``propose`` 是第一輪：檢查格式、自己抓網頁核對，通過後只存加鹽雜湊（地址、電話、
  縣市＋區、來源網站），不存值。排程第二天看不到第一輪查到什麼、用了哪些網站。
  同一家已經有第一輪（7 天內）就不再收第一輪，等第二輪。
- ``confirm`` 是第二輪：至少隔 12 小時、7 天內；來源網站不能和第一輪重複（重複就整筆作廢，
  免得一個個試出第一輪用了哪些網站）；地址的雜湊要一樣才收。電話要兩輪都核對到
  （或和已收的電話相同）才收，否則只收地址。
- 同名店在不同縣市或區分開存（審查員 3：台北和台南的甲麵館會互相覆蓋）。
- 已收的店第一輪查到不同的地址或電話：先停用舊資料，新值也要兩輪一致才生效；
  第二輪反而和舊資料一樣時恢復舊資料（查證日期不更新；第二輪電話不同就不留電話）。
  只是這輪沒核對到電話，不算「查到不同」。

每一輪的網頁核對（自己抓，只走 safe_fetch，只算 HTTP 2xx 的頁）：
- 至少兩個不同網站的頁面提到這家店（同一品牌的不同網域算一個）；
- 至少一頁的地址綁在店名上，而且那段有寫到區；電話要綁在店名上才算；
- 郵局、機關至少一個政府網站（.gov.tw、.gov.taipei）的頁面綁得上地址；
- 來源網址本身含有這家的地址或電話時不收（站內搜尋頁會把網址的字印回頁面）。
第一批不收醫療院所、銀行。每天最多寫 10 次。

每次寫入先記一行到只追加的 ``kb_places_events.jsonl``（含改前改後），再寫檔；``revert``
只撤目前狀態還等於那筆「改後」的異動。有 ``state/kb_places.disabled`` 時拒絕 propose／confirm
（revert 照樣可以用來救）。``stats``、``todo`` 只印數量和店名，不印地址、電話、來源。
結束碼：0 成功、3 已停用、4 今天到上限、5 兩輪不一致（作廢）、6 知識庫檔壞了、
7 網頁核對不到、10 其他拒收（參數錯誤是 argparse 的 2）。
正式指令沒有 --now、--state-dir：狀態檔固定在 state/（測試用 monkeypatch 改 _now、DEFAULT_STATE_DIR）。
段二待辦（審查員 2）：nominations 只驗格式，bot 回答前要用資料庫確認那則訊息真的在那個群組、
真的提到這家店，而且沒有被收回；段一期間收回的訊息查不到，提名照留（已知限制）。
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from urllib.parse import unquote
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_normalize as kbn  # noqa: E402

_TW = ZoneInfo("Asia/Taipei")
DEFAULT_STATE_DIR = BASE / "state"
DATA_NAME = "kb_places.json"
EVENTS_NAME = "kb_places_events.jsonl"
SALT_NAME = "kb_places.salt"
LOCK_NAME = "kb_places.lock"
DISABLED_NAME = "kb_places.disabled"

VERSION = 1
DAILY_CAP = 10
MIN_SITES = 2
MAX_SOURCES = 6
ROUND_GAP_SECONDS = 12 * 3600
PENDING_TTL_SECONDS = 7 * 86400
DAY_SECONDS = 86400
MAX_AGE_DAYS = 120
RECHECK_DAYS = 100
MAX_NOMINATIONS = 50
FETCH_DEADLINE = 10.0
FETCH_MAX_BYTES = 2 * 1024 * 1024
ACCEPT_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain"})
STATUSES = ("active", "suspended")
_COUNTED = frozenset({"propose", "activate", "restore", "mismatch", "overlap", "region_mismatch", "expire"})

EXIT_OK = 0
EXIT_DISABLED = 3
EXIT_CAP = 4
EXIT_MISMATCH = 5
EXIT_BAD_FILE = 6
EXIT_UNVERIFIED = 7
EXIT_REJECTED = 10

_GROUP_RE = re.compile(r"[CRU][0-9a-f]{32}")
_MESSAGE_RE = re.compile(r"[0-9]{5,25}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class BadFile(Exception):
    """知識庫檔格式不對：整份不用，也不寫。"""


class Rejected(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> int:
    """現在的 epoch 秒。正式指令沒有 --now（審查員 1、3：可以拿來跳過 12 小時和每日上限）；測試用 monkeypatch。"""
    return int(time.time())


# ── 檔案 ──────────────────────────────────────────────────────────────────────


def _empty() -> dict:
    return {"version": VERSION, "places": [], "pending": []}


def _require(cond: bool, why: str) -> None:
    if not cond:
        raise BadFile(why)


def _check_nominations(value) -> None:
    _require(isinstance(value, list) and len(value) <= MAX_NOMINATIONS, "nominations 不對")
    for pair in value:
        _require(
            isinstance(pair, list) and len(pair) == 2
            and isinstance(pair[0], str) and _GROUP_RE.fullmatch(pair[0]) is not None
            and isinstance(pair[1], str) and _MESSAGE_RE.fullmatch(pair[1]) is not None,
            "nominations 的群組或訊息編號不對",
        )


def _place_id(key: str, region: str) -> str:
    return hashlib.sha256(("place\x1f" + key + "\x1f" + region).encode("utf-8")).hexdigest()[:16]


def validate(data, *, semantic: bool = True) -> None:
    """整份檢查。任何一筆不合格就丟 BadFile：bot 和管理指令都整份不用。

    ``semantic=False`` 只看結構（給 revert 用：以後規則變嚴，舊資料不合新規則時還救得回來）。
    """
    _require(isinstance(data, dict) and data.get("version") == VERSION, "版本不對")
    places, pending = data.get("places"), data.get("pending")
    _require(isinstance(places, list) and isinstance(pending, list), "places／pending 不是清單")
    keys: set[tuple[str, str]] = set()
    for p in places:
        _require(isinstance(p, dict), "place 不是物件")
        for field in ("place_id", "name", "name_key", "kind", "address", "region", "phone", "phone_source",
                      "verified_on", "status", "addr_hash", "phone_hash"):
            _require(isinstance(p.get(field), str), f"place 缺 {field}")
        _require((p["name_key"], p["region"]) not in keys, "同一家店重複")
        keys.add((p["name_key"], p["region"]))
        _require(p["place_id"] == _place_id(p["name_key"], p["region"]), "place_id 不對")
        _require(p["status"] in STATUSES, "status 不對")
        _require(_DATE_RE.fullmatch(p["verified_on"]) is not None, "verified_on 不對")
        date.fromisoformat(p["verified_on"])
        _require(isinstance(p.get("sources"), list) and all(isinstance(u, str) for u in p["sources"]), "sources 不對")
        _check_nominations(p.get("nominations"))
        if semantic:
            name, _why = kbn.validate_name(p["name"], p["kind"])
            _require(p["kind"] in kbn.KINDS and name == p["name"], "店名或種類不合格")
            _require(p["name_key"] == kbn.name_key(p["name"]), "name_key 不對")
            _require(kbn.parse_address(p["address"]) == p["address"], "地址不是正規化寫法")
            _require(kbn.region(p["address"]) == p["region"], "region 和地址不一致")
            _require(p["phone"] == "" or kbn.parse_phone(p["phone"]) == p["phone"], "電話不是正規化寫法")
            _require((p["phone"] == "") == (p["phone_hash"] == "") == (p["phone_source"] == ""), "電話欄位不一致")
            _require(_HASH_RE.fullmatch(p["addr_hash"]) is not None, "addr_hash 不對")
            _require(p["phone_hash"] == "" or _HASH_RE.fullmatch(p["phone_hash"]) is not None, "phone_hash 不對")
            _require(len(p["sources"]) >= MIN_SITES and all(kbn.source_site(u)[0] for u in p["sources"]), "sources 不對")
    pending_keys: set[tuple[str, str]] = set()
    for q in pending:
        _require(isinstance(q, dict), "pending 不是物件")
        for field in ("name", "name_key", "kind", "region_hash", "addr_hash", "phone_hash"):
            _require(isinstance(q.get(field), str), f"pending 缺 {field}")
        _require((q["name_key"], q["region_hash"]) not in pending_keys, "pending 重複")
        pending_keys.add((q["name_key"], q["region_hash"]))
        _require(isinstance(q.get("proposed_at"), int) and not isinstance(q["proposed_at"], bool), "proposed_at 不對")
        hashes = q.get("domain_hashes")
        _require(isinstance(hashes, list) and all(isinstance(h, str) for h in hashes), "domain_hashes 不對")
        _check_nominations(q.get("nominations"))
        if semantic:
            _require(q["kind"] in kbn.KINDS and q["name_key"] == kbn.name_key(q["name"]), "pending 店名不對")
            for field in ("region_hash", "addr_hash"):
                _require(_HASH_RE.fullmatch(q[field]) is not None, f"pending {field} 不對")
            _require(q["phone_hash"] == "" or _HASH_RE.fullmatch(q["phone_hash"]) is not None, "pending phone_hash 不對")
            _require(len(hashes) >= MIN_SITES and all(_HASH_RE.fullmatch(h) for h in hashes), "domain_hashes 不對")


def load(path: Path, *, semantic: bool = True) -> dict:
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BadFile(f"讀不到或不是 JSON（{type(exc).__name__}）") from exc
    try:
        validate(data, semantic=semantic)
    except (ValueError, TypeError) as exc:
        raise BadFile(f"格式不對（{type(exc).__name__}）") from exc
    return data


def _write_json(path: Path, data: dict) -> None:
    """暫存檔＋os.replace，0600。呼叫的地方要先 validate。"""
    fd, tmp = tempfile.mkstemp(prefix=".kb_places.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _no_symlinks(*paths: Path) -> None:
    """狀態檔不准是符號連結（審查員 2：O_APPEND 會跟著連結寫到別的地方）。"""
    for path in paths:
        if path.is_symlink():
            raise Rejected(EXIT_BAD_FILE, f"{path.name} 是符號連結，先不寫入，請回報")


def _append_events(path: Path, events: list[dict]) -> None:
    """先記異動再寫檔：當機時最多多一筆沒生效的紀錄（revert 會因為狀態不符而拒絕），不會漏記。"""
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+", encoding="utf-8") as fh:
        size = os.fstat(fd).st_size
        # 上次寫到一半斷掉（結尾沒有換行）：先補換行，這次的紀錄才不會黏在斷掉的那行後面（審查員 1）
        if size and os.pread(fd, 1, size - 1) != b"\n":
            fh.write("\n")
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events = []
    # 寫到一半當機時，最後一行可能斷在多位元組字中間：那一行跳過，不要讓之後每個指令都失敗（審查員 1）
    for line in path.read_bytes().splitlines():
        with contextlib.suppress(ValueError):
            row = json.loads(line.decode("utf-8"))
            if isinstance(row, dict):
                events.append(row)
    return events


@contextlib.contextmanager
def _locked(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(state_dir / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


_SALT_RE = re.compile(r"[0-9a-f]{64}")


def _salt(state_dir: Path, data: dict) -> bytes:
    """鹽檔：第一次用時建（暫存檔＋link，不會留下空檔）；知識庫已有資料時鹽檔不見或壞了就拒絕。

    審查員 2：鹽檔被悄悄重建時，同樣的值算出不同的雜湊，重查會把有效資料停用。
    """
    path = state_dir / SALT_NAME
    _no_symlinks(path)
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if not _SALT_RE.fullmatch(value):
            raise Rejected(EXIT_BAD_FILE, "鹽檔格式不對，先不寫入，請回報")
        return value.encode("utf-8")
    if data["places"] or data["pending"]:
        raise Rejected(EXIT_BAD_FILE, "知識庫有資料但鹽檔不見了，先不寫入，請回報")
    fd, tmp = tempfile.mkstemp(prefix=".kb_salt.", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(secrets.token_hex(32))
            fh.flush()
            os.fsync(fh.fileno())
        os.link(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
    return path.read_text(encoding="utf-8").strip().encode("utf-8")


def _h(salt: bytes, *parts: str) -> str:
    return hashlib.sha256(salt + "\x1f".join(parts).encode("utf-8")).hexdigest()


def _day(ts: int) -> date:
    return datetime.fromtimestamp(ts, _TW).date()


def _age_days(place: dict, now: int) -> int:
    return (_day(now) - date.fromisoformat(place["verified_on"])).days


def is_fresh(place: dict, now: int) -> bool:
    """讀取當下判斷：status 是 active，而且查證後還沒超過 120 天（不靠排程去改狀態）。"""
    return place.get("status") == "active" and 0 <= _age_days(place, now) < MAX_AGE_DAYS


def _pending_expired(pending: dict, now: int) -> bool:
    return now - pending["proposed_at"] > PENDING_TTL_SECONDS


# ── 抓網頁 ────────────────────────────────────────────────────────────────────


def _decode(resp) -> str:
    raw = resp.content or b""
    declared = (resp.encoding or "").lower()
    if declared and declared not in ("iso-8859-1", "latin-1", "ascii"):
        with contextlib.suppress(LookupError):
            return raw.decode(declared, errors="replace")
    meta = re.search(rb"<meta[^>]+charset=[\"']?([A-Za-z0-9_\-]+)", raw[:4096], re.I)
    if meta:
        with contextlib.suppress(LookupError, UnicodeDecodeError):
            return raw.decode(meta.group(1).decode("ascii"), errors="strict")
    for encoding in ("utf-8", "cp950"):
        with contextlib.suppress(UnicodeDecodeError):
            return raw.decode(encoding)
    return raw.decode("utf-8", errors="replace")


def _fetch(url: str) -> tuple[str, list[str]]:
    """(轉址後的網址, 段落)。只走 safe_fetch：公開位址、逐跳檢查轉址、大小和時間上限；非 2xx 丟例外。"""
    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))
    import safe_fetch

    resp = safe_fetch.fetch(
        url, timeout=5, deadline=FETCH_DEADLINE, max_bytes=FETCH_MAX_BYTES, truncate=True,
        accept_types=ACCEPT_TYPES,
    )
    if not 200 <= int(resp.status_code) < 300:
        raise ConnectionError(f"HTTP {resp.status_code}")
    content_type = (resp.headers.get("Content-Type") or "text/html").split(";", 1)[0].strip().lower()
    return resp.url or url, kbn.page_blocks(_decode(resp), content_type)


# ── 一輪查證 ──────────────────────────────────────────────────────────────────


def _nominations(values: list[str] | None) -> list[list[str]]:
    found: list[list[str]] = []
    for value in values or []:
        group_id, _, message_id = (value or "").partition(":")
        if not _GROUP_RE.fullmatch(group_id) or not _MESSAGE_RE.fullmatch(message_id):
            raise Rejected(EXIT_REJECTED, "--nominate 要寫成 群組代號:訊息編號（照掃描輸出）")
        if [group_id, message_id] not in found:
            found.append([group_id, message_id])
    return found


def _merge_nominations(*lists: list[list[str]]) -> list[list[str]]:
    merged: list[list[str]] = []
    for items in lists:
        for pair in items or []:
            if list(pair) not in merged:
                merged.append(list(pair))
    return merged[-MAX_NOMINATIONS:]


def _url_leaks(url: str, address: str, phone: str | None) -> bool:
    """網址本身含有這家的電話或地址：站內搜尋頁會把網址裡的字印回頁面，等於自己核對自己。

    轉址前後的網址都要查（審查員 2）；電話也比對 +886 寫法（去掉開頭的 0）。
    """
    text = unquote(unquote(url or ""))
    digits = re.sub(r"[^0-9]", "", text)
    if phone and (phone in digits or phone[1:] in digits):
        return True
    parts = kbn.address_parts(address)
    return bool(parts) and (parts[2] + parts[3]) in kbn.address_text(text)


def check_round(args, salt: bytes) -> dict:
    """格式檢查＋自己抓網頁核對。通過回這一輪的結果；不通過丟 Rejected。"""
    if args.kind not in kbn.KINDS:
        raise Rejected(EXIT_REJECTED, f"種類只能是：{'、'.join(kbn.KINDS)}")
    name, why = kbn.validate_name(args.name, args.kind)
    if not name:
        raise Rejected(EXIT_REJECTED, why)
    if kbn.MEDICAL_RE.search(name) or kbn.BANK_RE.search(name):
        raise Rejected(EXIT_REJECTED, "第一批不收醫療院所和銀行")
    address = kbn.parse_address(args.address or "")
    if not address:
        raise Rejected(EXIT_REJECTED, "地址拆不全：要有縣市、區、路（段巷弄）和號")
    phone = None
    if args.phone:
        phone = kbn.parse_phone(args.phone)
        if not phone:
            raise Rejected(EXIT_REJECTED, "電話格式不對（要含區碼）")
    sources = list(dict.fromkeys(u.strip() for u in (args.source or []) if u and u.strip()))
    if not MIN_SITES <= len(sources) <= MAX_SOURCES:
        raise Rejected(EXIT_REJECTED, f"每一輪要 {MIN_SITES}–{MAX_SOURCES} 個來源網址")
    for url in sources:
        site, _host, why = kbn.source_site(url)
        if not site:
            raise Rejected(EXIT_REJECTED, f"來源不收：{why}")
        if _url_leaks(url, address, phone):
            raise Rejected(EXIT_REJECTED, "來源網址裡含有這家的地址或電話（像站內搜尋頁），換別的頁面")
    supporting: set[str] = set()
    address_ok = city_ok = gov_ok = False
    phone_url = ""
    for url in sources:
        try:
            final_url, blocks = _fetch(url)
        except Exception:  # noqa: BLE001 - 抓不到、非 2xx 的頁（例如要登入）不算網站
            continue
        site, host, why = kbn.source_site(final_url)
        if not site:
            raise Rejected(EXIT_REJECTED, f"轉址後的來源不收：{why}")
        if _url_leaks(final_url, address, phone):
            raise Rejected(EXIT_REJECTED, "轉址後的網址裡含有這家的地址或電話（像站內搜尋頁），換別的頁面")
        support = kbn.page_supports(blocks, name, address=address, phone=phone)
        if not support.mentioned:
            continue
        supporting.add(site)
        if support.address and support.address_full:
            address_ok = True
            city_ok = city_ok or support.address_city
            gov_ok = gov_ok or kbn.is_gov_host(host)
        if support.phone and not phone_url:
            phone_url = final_url
    if len(supporting) < MIN_SITES:
        raise Rejected(EXIT_UNVERIFIED, "提到這家店、而且抓得到的網站不到兩個")
    if not address_ok:
        raise Rejected(EXIT_UNVERIFIED, "抓到的網頁裡，地址沒有緊接在店名後面（或那段沒寫到區）")
    if not city_ok:  # 中正、信義、大安、東區…好幾個縣市都有：至少一頁要寫到縣市（審查員 1）
        raise Rejected(EXIT_UNVERIFIED, "抓到的網頁裡，沒有一頁在店名後面的地址寫到縣市")
    if args.kind in kbn.OFFICE_KINDS and not gov_ok:
        raise Rejected(EXIT_UNVERIFIED, "郵局、機關要有一個政府網站的頁面寫著這個地址")
    key = kbn.name_key(name)
    area = kbn.region(address)
    kept_phone = phone if phone_url else ""
    return {
        "name": name,
        "name_key": key,
        "kind": args.kind,
        "address": address,
        "region": area,
        "phone": kept_phone,
        "phone_source": phone_url,
        "phone_dropped": bool(phone and not kept_phone),
        "sources": sources,
        "region_hash": _h(salt, "region", key, area),
        "addr_hash": _h(salt, "addr", key, address),
        "phone_hash": _h(salt, "phone", key, kept_phone) if kept_phone else "",
        "domain_hashes": sorted({_h(salt, "site", s) for s in supporting}),
    }


# ── 指令 ──────────────────────────────────────────────────────────────────────


def _paths(state_dir: Path) -> tuple[Path, Path]:
    return state_dir / DATA_NAME, state_dir / EVENTS_NAME


def _event(action: str, now: int, **fields) -> dict:
    return {"id": uuid.uuid4().hex, "ts": now, "action": action, **fields}


def _writes_today(events: list[dict], now: int) -> int:
    today = _day(now)
    return sum(1 for e in events if e.get("action") in _COUNTED and isinstance(e.get("ts"), int) and _day(e["ts"]) == today)


def _pending_record(key: str, region_hash: str) -> str:
    return key + "|" + region_hash


def _find_place(data: dict, key: str, area: str) -> dict | None:
    return next((p for p in data["places"] if p["name_key"] == key and p["region"] == area), None)


def _find_pending(data: dict, key: str, region_hash: str) -> dict | None:
    return next((q for q in data["pending"] if q["name_key"] == key and q["region_hash"] == region_hash), None)


def _drop_pending(data: dict, key: str, region_hash: str) -> None:
    data["pending"] = [q for q in data["pending"] if not (q["name_key"] == key and q["region_hash"] == region_hash)]


def _check_disabled(state_dir: Path) -> None:
    if (state_dir / DISABLED_NAME).exists():
        raise Rejected(EXIT_DISABLED, "知識庫已停用（state/kb_places.disabled），這次不寫入")


def _prepare_write(state_dir: Path, now: int) -> tuple[dict, bytes]:
    _check_disabled(state_dir)
    data_path, events_path = _paths(state_dir)
    _no_symlinks(data_path, events_path)
    try:
        data = load(data_path)
    except BadFile as exc:
        raise Rejected(EXIT_BAD_FILE, f"知識庫檔格式不對：{exc}；先不寫入，請回報") from exc
    if _writes_today(_read_events(events_path), now) >= DAILY_CAP:
        raise Rejected(EXIT_CAP, f"今天已經寫了 {DAILY_CAP} 次，明天再繼續")
    return data, _salt(state_dir, data)


def _commit(state_dir: Path, data: dict, events: list[dict]) -> None:
    """寫入前再看一次停用檔：抓網頁要幾秒，期間建了停用檔也不寫（審查員 2）。"""
    _check_disabled(state_dir)
    data_path, events_path = _paths(state_dir)
    validate(data)
    _append_events(events_path, events)
    _write_json(data_path, data)


def _phones_compatible(a: str, b: str) -> bool:
    """沒核對到電話（空的）不算不同。"""
    return not a or not b or a == b


def cmd_propose(args, state_dir: Path, now: int) -> str:
    with _locked(state_dir):
        data, salt = _prepare_write(state_dir, now)
        result = check_round(args, salt)
        nominations = _nominations(args.nominate)
        key, area, region_hash = result["name_key"], result["region"], result["region_hash"]
        existing = _find_pending(data, key, region_hash)
        if existing is not None and not _pending_expired(existing, now):
            raise Rejected(EXIT_REJECTED, "這家已經做過第一輪，等第二輪（todo 會列出）")
        _drop_pending(data, key, region_hash)
        place = _find_place(data, key, area)
        notes = []
        events = []
        if place is not None:
            same = place["addr_hash"] == result["addr_hash"] and _phones_compatible(place["phone_hash"], result["phone_hash"])
            if not same:
                if place["status"] == "active":
                    before = copy.deepcopy(place)
                    place["status"] = "suspended"
                    events.append(_event("suspend", now, record=place["place_id"], before=before, after=copy.deepcopy(place)))
                    notes.append("查到的和知識庫裡的不同，舊資料先停用，等第二輪")
        pending = {
            "name": result["name"], "name_key": key, "kind": result["kind"],
            "region_hash": region_hash, "addr_hash": result["addr_hash"], "phone_hash": result["phone_hash"],
            "domain_hashes": result["domain_hashes"], "proposed_at": now,
            "nominations": _merge_nominations(nominations),
        }
        data["pending"].append(pending)
        events.append(_event("propose", now, record=_pending_record(key, region_hash), after=copy.deepcopy(pending)))
        _commit(state_dir, data, events)
    ready = datetime.fromtimestamp(now + ROUND_GAP_SECONDS, _TW).strftime("%m/%d %H:%M")
    if result["phone_dropped"]:
        notes.append("電話沒有在抓到的網頁上緊接在店名後面，這輪只算地址")
    return "\n".join([f"第一輪記下了：{result['name']}。{ready} 之後才能做第二輪，第二輪要換別的網站。"] + notes)


def cmd_confirm(args, state_dir: Path, now: int) -> tuple[int, str]:
    with _locked(state_dir):
        data, salt = _prepare_write(state_dir, now)
        result = check_round(args, salt)
        nominations = _nominations(args.nominate)
        key, area, region_hash = result["name_key"], result["region"], result["region_hash"]
        pending = _find_pending(data, key, region_hash)
        if pending is None:
            others = [q for q in data["pending"] if q["name_key"] == key]
            if others:
                # 同名的第一輪在別的縣市或區：整筆作廢並計次，免得換區重試試出第一輪的地址（審查員 2）。
                data["pending"] = [q for q in data["pending"] if q["name_key"] != key]
                _commit(state_dir, data, [_event("region_mismatch", now, record=key, before=copy.deepcopy(others))])
                return EXIT_MISMATCH, f"第二輪的縣市或區和第一輪不同，這家作廢：{result['name']}。下次從第一輪重來。"
            raise Rejected(EXIT_REJECTED, "這家還沒有第一輪，先用 propose")
        if _pending_expired(pending, now):
            _drop_pending(data, key, region_hash)
            _commit(state_dir, data, [_event("expire", now, record=_pending_record(key, region_hash), before=copy.deepcopy(pending))])
            raise Rejected(EXIT_REJECTED, "第一輪已經超過 7 天，作廢，下次重新從第一輪開始")
        if now - pending["proposed_at"] < ROUND_GAP_SECONDS:
            raise Rejected(EXIT_REJECTED, "兩輪至少要隔 12 小時")
        place = _find_place(data, key, area)
        _drop_pending(data, key, region_hash)
        record = _pending_record(key, region_hash)
        if set(result["domain_hashes"]) & set(pending["domain_hashes"]):
            _commit(state_dir, data, [_event("overlap", now, record=record, before=copy.deepcopy(pending))])
            return EXIT_MISMATCH, f"第二輪用到第一輪的網站，這家作廢：{result['name']}。下次從第一輪重來，第二輪要換網站。"
        today = _day(now).isoformat()
        if result["addr_hash"] == pending["addr_hash"]:
            phone_ok = bool(result["phone_hash"]) and (
                result["phone_hash"] == pending["phone_hash"]
                or (place is not None and result["phone_hash"] == place["phone_hash"])
            )
            after = {
                "place_id": _place_id(key, area), "name": result["name"], "name_key": key, "kind": result["kind"],
                "address": result["address"], "region": area,
                "phone": result["phone"] if phone_ok else "",
                "phone_source": result["phone_source"] if phone_ok else "",
                "phone_hash": result["phone_hash"] if phone_ok else "",
                "addr_hash": result["addr_hash"], "sources": result["sources"], "verified_on": today,
                "status": "active",
                "nominations": _merge_nominations(place["nominations"] if place else [],
                                                  pending["nominations"], nominations),
            }
            before = copy.deepcopy(place) if place else None
            data["places"] = [p for p in data["places"] if p is not place] + [after]
            _commit(state_dir, data, [
                _event("activate", now, record=after["place_id"], before=before, after=copy.deepcopy(after)),
            ])
            tail = "" if phone_ok else "（電話沒有兩輪都核對到，只收地址）"
            return EXIT_OK, f"查證完成，收進知識庫：{result['name']}{tail}"
        if place is not None and place["addr_hash"] == result["addr_hash"]:
            # 第一輪查到的和舊資料不同，第二輪又和舊資料一樣：第一輪看錯了，恢復舊資料。
            # 只多一輪不算重新查證，所以 verified_on 不更新；第二輪的電話和舊的不同時，電話不留。
            before = copy.deepcopy(place)
            place["status"] = "active"
            if result["phone_hash"] and place["phone_hash"] and result["phone_hash"] != place["phone_hash"]:
                place["phone"] = place["phone_source"] = place["phone_hash"] = ""
            place["nominations"] = _merge_nominations(place["nominations"], pending["nominations"], nominations)
            _commit(state_dir, data, [_event("restore", now, record=place["place_id"], before=before, after=copy.deepcopy(place))])
            return EXIT_OK, f"第二輪和原本的資料一樣，恢復原本那筆：{result['name']}"
        events = []
        note = ""
        if place is not None and place["status"] == "active":
            # 重查時第二輪查到別的值：資料可能已經變了，先停用，等下一次兩輪都一致。
            before = copy.deepcopy(place)
            place["status"] = "suspended"
            events.append(_event("suspend", now, record=place["place_id"], before=before, after=copy.deepcopy(place)))
            note = "舊資料先停用。"
        events.append(_event("mismatch", now, record=record, before=copy.deepcopy(pending)))
        _commit(state_dir, data, events)
        return EXIT_MISMATCH, f"兩輪查到的地址不同，這次作廢：{result['name']}。{note}下次重新從第一輪開始。"


def cmd_revert(args, state_dir: Path, now: int) -> str:
    """撤回一筆異動：只有目前狀態還等於那筆的「改後」時才撤（停用檔存在時也可以用：這是救援用的）。

    給 Andrew 或開發對話手動救援用，查證排程不准用（守門程式也擋）。原檔本來合格時，撤回後也要
    整份合格，還原的改前資料雜湊也要對得上；原檔已經不合新規則時才只看結構（審查員 2、3）。
    """
    with _locked(state_dir):
        data_path, events_path = _paths(state_dir)
        _no_symlinks(data_path, events_path)
        try:
            data = load(data_path, semantic=False)
        except BadFile as exc:
            raise Rejected(EXIT_BAD_FILE, f"知識庫檔格式不對：{exc}") from exc
        try:
            validate(data)
            was_valid = True
        except (BadFile, ValueError, TypeError):
            was_valid = False
        events = _read_events(events_path)
        target = next((e for e in events if e.get("id") == args.event), None)
        if target is None:
            raise Rejected(EXIT_REJECTED, "找不到這筆異動")
        if any(e.get("action") == "revert" and e.get("reverts") == args.event for e in events):
            raise Rejected(EXIT_REJECTED, "這筆異動已經撤回過了")
        action, record, after = target.get("action"), target.get("record"), target.get("after")
        if action == "propose":
            current = next((q for q in data["pending"] if _pending_record(q["name_key"], q["region_hash"]) == record), None)
            if current != after:
                raise Rejected(EXIT_REJECTED, "之後又有變動，不能撤回這筆")
            data["pending"] = [q for q in data["pending"] if q is not current]
        elif action in ("activate", "restore", "suspend"):
            current = next((p for p in data["places"] if p.get("place_id") == record), None)
            if current != after:
                raise Rejected(EXIT_REJECTED, "之後又有變動，不能撤回這筆")
            data["places"] = [p for p in data["places"] if p is not current]
            if target.get("before"):
                data["places"].append(target["before"])
        else:
            raise Rejected(EXIT_REJECTED, "這種異動不能撤回")
        restored = target.get("before") if action != "propose" else None
        if restored and was_valid:
            salt = _salt(state_dir, data)
            if (restored.get("addr_hash") != _h(salt, "addr", restored.get("name_key", ""), restored.get("address", ""))
                    or (restored.get("phone") and restored.get("phone_hash")
                        != _h(salt, "phone", restored.get("name_key", ""), restored["phone"]))):
                raise Rejected(EXIT_REJECTED, "這筆異動的改前資料對不上雜湊，不能撤回，請回報")
        try:
            validate(data, semantic=was_valid)
        except (BadFile, ValueError, TypeError) as exc:
            raise Rejected(EXIT_REJECTED, f"撤回後的資料不合格，不能撤回：{exc}") from exc
        _append_events(events_path, [_event("revert", now, record=record, reverts=args.event)])
        _write_json(data_path, data)
    return "已撤回。"


def _load_for_read(state_dir: Path) -> dict:
    try:
        return load(_paths(state_dir)[0])
    except BadFile as exc:
        raise Rejected(EXIT_BAD_FILE, f"知識庫檔格式不對：{exc}") from exc


def cmd_todo(args, state_dir: Path, now: int) -> str:
    data = _load_for_read(state_dir)
    live = [q for q in data["pending"] if not _pending_expired(q, now)]
    ready = [q for q in live if now - q["proposed_at"] >= ROUND_GAP_SECONDS]
    lines = [f"第二輪可以做：{q['name']}（{q['kind']}）" for q in ready]
    if len(live) > len(ready):
        lines.append(f"第一輪做完、還沒滿 12 小時：{len(live) - len(ready)} 筆")
    if len(data["pending"]) > len(live):
        lines.append(f"第一輪超過 7 天沒做第二輪（會作廢）：{len(data['pending']) - len(live)} 筆")
    if args.recheck or _day(now).weekday() == 0:  # 星期一自動列（台北時間），排程不用自己算星期幾
        pending_names = {q["name_key"] for q in live}
        for p in data["places"]:
            if p["name_key"] in pending_names:
                continue
            if p["status"] == "suspended" or _age_days(p, now) >= RECHECK_DAYS:
                lines.append(f"要重查：{p['name']}（{p['kind']}）")
    return "\n".join(lines) if lines else "沒有待辦。"


def cmd_stats(args, state_dir: Path, now: int) -> str:
    data = _load_for_read(state_dir)
    places = data["places"]
    fresh = sum(1 for p in places if is_fresh(p, now))
    expired = sum(1 for p in places if p["status"] == "active" and not is_fresh(p, now))
    suspended = sum(1 for p in places if p["status"] == "suspended")
    with_phone = sum(1 for p in places if is_fresh(p, now) and p["phone"])
    live = [q for q in data["pending"] if not _pending_expired(q, now)]
    ready = sum(1 for q in live if now - q["proposed_at"] >= ROUND_GAP_SECONDS)
    events = _read_events(_paths(state_dir)[1])
    # 最近 24 小時（每日維護 09:45 跑 stats，查證排程 14:10 才跑：用「今天」會永遠是 0，審查員 1）
    recent = [e for e in events if isinstance(e.get("ts"), int) and now - DAY_SECONDS < e["ts"] <= now]
    counts = {a: sum(1 for e in recent if e.get("action") == a)
              for a in ("propose", "activate", "restore", "mismatch", "overlap", "region_mismatch", "expire", "suspend")}
    disabled = "（已停用）" if (state_dir / DISABLED_NAME).exists() else ""
    return (
        f"知識庫{disabled}：有效 {fresh} 家（含電話 {with_phone}）、過期 {expired}、停用 {suspended}；"
        f"等第二輪 {len(live)}（可做 {ready}）。"
        f"最近 24 小時：第一輪 {counts['propose']}、收進 {counts['activate']}、恢復 {counts['restore']}、"
        f"作廢 {counts['mismatch'] + counts['overlap'] + counts['region_mismatch']}、第一輪逾期 {counts['expire']}、"
        f"停用 {counts['suspend']}；今天寫入 {_writes_today(events, now)}/{DAILY_CAP}。"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("propose", "confirm"):
        p = sub.add_parser(name)
        p.add_argument("--name", required=True)
        p.add_argument("--kind", required=True, choices=kbn.KINDS)
        p.add_argument("--address", required=True)
        p.add_argument("--phone", default=None)
        p.add_argument("--source", action="append", default=[])
        p.add_argument("--nominate", action="append", default=[], help="群組代號:訊息編號（照掃描輸出）")
    revert = sub.add_parser("revert")
    revert.add_argument("--event", required=True)
    todo = sub.add_parser("todo")
    todo.add_argument("--recheck", action="store_true", help="手動用：不是星期一也列出要重查的（星期一自動列）")
    sub.add_parser("stats")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    now = _now()
    state_dir = Path(DEFAULT_STATE_DIR)
    try:
        if args.command == "propose":
            print(cmd_propose(args, state_dir, now))
            return EXIT_OK
        if args.command == "confirm":
            code, message = cmd_confirm(args, state_dir, now)
            print(message)
            return code
        handler = {"revert": cmd_revert, "todo": cmd_todo, "stats": cmd_stats}[args.command]
        print(handler(args, state_dir, now))
        if args.command in ("stats", "todo") and (state_dir / DISABLED_NAME).exists():
            return EXIT_DISABLED  # 停用時查證排程在第 1 步就停（審查員 1、4）
        return EXIT_OK
    except Rejected as exc:
        print(f"kb_places_admin: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
