"""字卡大字版網頁（2026-10-16）：股票、FCN 卡片送出時同時存一頁大字版，卡片加「🔍 看大字版」。

Andrew：「把字卡（股票/FCN…）部分做微調，由於字實在太小，除非用電腦看，否則很吃力，能把它
同時弄成網頁版嗎？除了字卡本身，也提供網頁讓我能點進去看大字版」。

- 網頁只放卡片上原本的字（送出前整張卡已過 output_validator），不另外加資料。
- 對外網址是 cloudflared quick tunnel（/tmp/cloudflared_line_bot_url.txt），重啟就換網址：
  舊卡片的「看大字版」會打不開（連到的是 Cloudflare 自己的錯誤頁），要重新叫一次卡片。
  /tmp 誰都能寫：檔案要是自己的、0600、一般檔才採用。
- 卡片白名單（main._flex_card_texts）只認這個程序剛發出的頁面網址（page_action_ok：記憶體
  裡的清單，不讀檔），送卡前和 _reply 裡兩次驗證的結果一定相同。
- 頁面網址帶 22 字隨機 token（secrets.token_urlsafe(16)；這台 Mac 的磁碟不分大小寫，實際約 107 位元以上，
  一樣猜不到）；檔案 0600，超過 TTL 天數就 404；沒有 JavaScript、不載任何外部資源、不給搜尋引擎收錄。
  TODO(2026-10-17 審查延後)：一般頁的檔名就是 token；健康頁（下面）已改存 sha256(token)，一般頁還沒改。
- 不 import main（main 會 import 這裡；import 失敗時 main 不掛路由、不加按鈕、拒收 uri）。
- 只有股票卡、FCN 評估卡、盤勢卡、爸媽健康卡做網頁（main._reply_command_card 的 page=True 加 altText 正面表列）；
  FCN 確認卡不做。
- 爸媽健康卡（2026-10-23，Andrew：「健康字卡也新增大字的網頁連結」，推翻 10/17 的排除）：
  publish(kind="health") 另存 health/ 子目錄，檔名是 sha256(token)（大小寫也算數、檔名不等於憑證），
  預設 24 小時過期（LINE_BOT_CARD_PAGE_HEALTH_TTL_HOURS，1–72）、最多 100 頁；LINE_BOT_CARD_PAGE_HEALTH=0
  只關健康頁（股票、FCN 照舊）。頁面不放家人名字：存檔前再檢查一次，有別名或「Andrew」就不存。
  刪除紀錄、暫停、收回訊息時 purge_health_pages() 全部清掉；過期的由每天的健康背景執行緒順手清（prune_health_pages）。
"""

from __future__ import annotations

import copy
import hashlib
import html
import json
import logging
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

logger = logging.getLogger(__name__)

ENABLED_ENV = "LINE_BOT_CARD_PAGES"  # 0／false／no／off = 不產生、不加按鈕、舊頁一律 404
TTL_ENV = "LINE_BOT_CARD_PAGE_TTL_DAYS"
DIR_ENV = "LINE_BOT_CARD_PAGES_DIR"  # 測試隔離用
BASE_URL_ENV = "LINE_BOT_CARD_PAGES_BASE_URL"  # 有固定網域時用；平常不設，讀 tunnel 檔
HEALTH_ENV = "LINE_BOT_CARD_PAGE_HEALTH"  # 0／false／no／off = 爸媽健康卡不做網頁、舊的健康頁一律 404（股票、FCN 照舊）
HEALTH_TTL_ENV = "LINE_BOT_CARD_PAGE_HEALTH_TTL_HOURS"
TUNNEL_URL_FILE = "/tmp/cloudflared_line_bot_url.txt"
DEFAULT = "default"
HEALTH = "health"  # publish(kind=…) 與子目錄名
DEFAULT_TTL_DAYS = 7
DEFAULT_HEALTH_TTL_HOURS = 24
MAX_PAGES = 500
HEALTH_MAX_PAGES = 100
TMP_MAX_AGE_SEC = 3600
MAX_PAGE_BYTES = 1_000_000
BUTTON_LABEL = "🔍 看大字版"
# LINE 的限制：action label ≤ 20 字、uri ≤ 1000 字；這個 repo 的卡片上限 30 KB（test_fcn_eval.py）
MAX_LABEL_CHARS = 20
MAX_URI_CHARS = 1000
MAX_CARD_BYTES = 30 * 1024

_OFF = frozenset({"0", "false", "no", "off"})
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{22}")
_PAGE_NAME_RE = re.compile(r"[A-Za-z0-9_-]{22}\.html")
_HEALTH_NAME_RE = re.compile(r"[0-9a-f]{64}\.html")  # sha256(token)
# 健康頁上可以出現的稱謂（其他家人的別名一律不行）
_HEALTH_PAGE_TITLES = frozenset({"爸爸", "媽媽", "爸媽", "家人"})
_TMP_PREFIX = ".card-"
_TMP_NAME_RE = re.compile(r"\.card-[a-z0-9_]{8}\.tmp")  # tempfile.mkstemp 的亂數檔名
# tunnel 主機：和 health_check.sh、preflight_check.py 一樣排除 api.trycloudflare.com；只收小寫
# TODO(2026-10-16 審查延後)：加漂移測試，確認這裡和 preflight_check.TUNNEL_URL_PATTERN／RESERVED_TRYCLOUDFLARE_HOSTS 一致。
_TUNNEL_BASE_RE = re.compile(r"https://(?!api\.trycloudflare\.com)[a-z0-9][a-z0-9-]*\.trycloudflare\.com")
_OVERRIDE_BASE_RE = re.compile(r"https://[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[0-9]{1,5})?")
_PAGE_URI_RE = re.compile(
    r"(https://[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[0-9]{1,5})?/card/[A-Za-z0-9_-]{22})(?:#b[1-9][0-9]?)?"
)
_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?")
_SIZE_PX = {"xxs": 17, "xs": 18, "sm": 20, "md": 22, "lg": 25, "xl": 30, "xxl": 34, "3xl": 40, "4xl": 48, "5xl": 56}
_SPACE_PX = {"none": 0, "xs": 4, "sm": 8, "md": 14, "lg": 20, "xl": 26, "xxl": 34}
_MAX_DEPTH = 24
_PARTS = ("header", "body", "footer")  # hero 是圖，卡片白名單本來就沒有圖
_TZ = ZoneInfo("Asia/Taipei")

_MINTED: OrderedDict[str, None] = OrderedDict()  # 這個程序發出過的頁面網址（不含錨點）
_MINTED_LOCK = threading.Lock()
_MINTED_MAX = 256

_HEADERS = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}


def enabled() -> bool:
    """開關；在 Cloud Run（K_SERVICE）上一律關：頁面只存在單一台機器上。"""
    if os.environ.get("K_SERVICE"):
        return False
    return os.environ.get(ENABLED_ENV, "1").strip().lower() not in _OFF


def health_enabled() -> bool:
    """爸媽健康頁的開關（2026-10-23 GP2 N6）：整個功能關掉時也算關；關掉時不存、舊頁 404、背景清理全部刪掉。"""
    return enabled() and os.environ.get(HEALTH_ENV, "1").strip().lower() not in _OFF


def ttl_seconds() -> int:
    try:
        days = float(os.environ.get(TTL_ENV, "").strip() or DEFAULT_TTL_DAYS)
    except ValueError:
        days = DEFAULT_TTL_DAYS
    if days != days:  # NaN
        days = DEFAULT_TTL_DAYS
    return int(min(max(days, 1.0), 30.0) * 86400)


def health_ttl_seconds() -> int:
    """健康頁的 TTL：小時，預設 24、夾在 1–72（寫錯、NaN 都當預設）。"""
    try:
        hours = float(os.environ.get(HEALTH_TTL_ENV, "").strip() or DEFAULT_HEALTH_TTL_HOURS)
    except ValueError:
        hours = DEFAULT_HEALTH_TTL_HOURS
    if hours != hours:  # NaN
        hours = DEFAULT_HEALTH_TTL_HOURS
    return int(min(max(hours, 1.0), 72.0) * 3600)


def health_ttl_hours() -> int:
    """頁尾和 /健康 說明寫的小時數（無條件進位：寫的是「最多保留」）。"""
    return -(-health_ttl_seconds() // 3600)


def pages_dir() -> Path:
    raw = os.environ.get(DIR_ENV, "").strip()
    if raw:  # 轉成絕對路徑但不解 symlink：目錄本身是 symlink 就要被 _dir_ok 擋下
        return Path(os.path.abspath(os.path.expanduser(raw)))
    return Path(__file__).resolve().with_name("state") / "card_pages"


def _health_name(token: str) -> str:
    """健康頁的檔名：sha256(token)。檔名不再等於憑證，大小寫不同的 token 也對不到同一頁。"""
    return hashlib.sha256(token.encode("ascii")).hexdigest() + ".html"


def _private_owned(info: os.stat_result, *, max_mode: int) -> bool:
    return info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) & ~max_mode == 0


def _dir_ok(directory: Path) -> bool:
    """頁面目錄：不是 symlink、是目錄、屬於自己、權限 0700 以內（檔名就是 token，別人不能列出來）。"""
    try:
        info = os.lstat(directory)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and _private_owned(info, max_mode=0o700)


def _read_tunnel_file() -> str:
    """/tmp 誰都能寫：只採用自己的、0600 以內的一般檔，最多讀 256 bytes；FIFO、symlink 都不碰。"""
    fd = os.open(TUNNEL_URL_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not _private_owned(info, max_mode=0o600):
            return ""
        return os.read(fd, 256).decode("utf-8")
    finally:
        os.close(fd)


def _override_base() -> str | None:
    """env 指定的網址；有設但格式不對 → ""（不退回 tunnel，當作沒有網址）。

    主機名不能有連續的點、port 要在 1–65535：LINE 收到壞網址會整張卡回 400，沒有固定句可退。
    """
    raw = os.environ.get(BASE_URL_ENV, "").strip().rstrip("/")
    if not raw:
        return None
    if not _OVERRIDE_BASE_RE.fullmatch(raw) or ".." in raw:
        return ""
    port = raw.rsplit(":", 1)[1] if raw.count(":") == 2 else None
    return "" if port is not None and not 1 <= int(port) <= 65535 else raw


def public_base_url() -> str | None:
    """對外網址：env 指定的優先，否則讀 cloudflared 寫的檔（每次都重讀）；不合格就 None。"""
    override = _override_base()
    if override is not None:
        return override or None
    try:
        raw = _read_tunnel_file().strip().rstrip("/")
    except (OSError, UnicodeError):
        return None
    return raw if _TUNNEL_BASE_RE.fullmatch(raw) else None


def _remember(page_url: str) -> None:
    with _MINTED_LOCK:
        _MINTED[page_url] = None
        _MINTED.move_to_end(page_url)
        while len(_MINTED) > _MINTED_MAX:
            _MINTED.popitem(last=False)


def page_action_ok(action: Any) -> bool:
    """卡片白名單用：只有 type／label／uri、label 是「🔍 看大字版」、uri 是這個程序剛發出的頁面（可帶 #bN）。"""
    if not isinstance(action, dict) or set(action) != {"type", "label", "uri"} or action.get("type") != "uri":
        return False
    uri = action.get("uri")
    if action.get("label") != BUTTON_LABEL or not isinstance(uri, str) or len(uri) > MAX_URI_CHARS:
        return False
    match = _PAGE_URI_RE.fullmatch(uri)
    if match is None:
        return False
    with _MINTED_LOCK:
        return match.group(1) in _MINTED


def fits_line_limits(card: dict) -> bool:
    """加了按鈕的卡還在 LINE 的限制內（送出時 LINE 回 400 整張卡就沒了，沒有固定句可退）。"""
    try:
        size = len(json.dumps(card, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return False
    return (
        size <= MAX_CARD_BYTES
        and len(BUTTON_LABEL) <= MAX_LABEL_CHARS
        and all(len(node["action"]["uri"]) <= MAX_URI_CHARS for node in _page_buttons(card))
    )


def _bubbles(card: Any) -> list[dict]:
    """卡片裡的 bubble，依序；網頁的 #bN 和按鈕的錨點都用這一份編號。"""
    if not isinstance(card, dict):
        return []
    if card.get("type") == "bubble":
        return [card]
    contents = card.get("contents") if card.get("type") == "carousel" else None
    return [b for b in contents if isinstance(b, dict) and b.get("type") == "bubble"] if isinstance(contents, list) else []


def bubble_count(card: Any) -> int:
    """卡片有幾張 bubble（健康卡的網頁版要和 LINE 卡一樣多，錨點才對得上）。"""
    return len(_bubbles(card))


def _page_buttons(card: Any) -> list[dict]:
    found = []
    for bubble in _bubbles(card):
        footer = bubble.get("footer")
        for node in footer.get("contents", []) if isinstance(footer, dict) else []:
            action = node.get("action") if isinstance(node, dict) else None
            if isinstance(action, dict) and action.get("type") == "uri":
                found.append(node)
    return found


# ── Flex → HTML ───────────────────────────────────────────────────────────────


def _render_node(node: Any, out: list[str], depth: int = 0) -> None:
    """只畫字、分隔線和排版的 box；按鈕和可以點的 box（有 action）在網頁上按了沒用，不畫。"""
    if not isinstance(node, dict) or depth > _MAX_DEPTH or "action" in node:
        return
    kind = node.get("type")
    style: list[str] = []
    margin = _SPACE_PX.get(node.get("margin")) if isinstance(node.get("margin"), str) else None
    if margin:
        style.append(f"margin-top:{margin}px")
    if kind == "box":
        gap = _SPACE_PX.get(node.get("spacing")) if isinstance(node.get("spacing"), str) else None
        if gap:
            style.append(f"gap:{gap}px")
        css = "row" if node.get("layout") in ("horizontal", "baseline") else "col"
        out.append(f'<div class="{css}" style="{";".join(style)}">')
        contents = node.get("contents")
        for child in contents if isinstance(contents, list) else []:
            _render_node(child, out, depth + 1)
        out.append("</div>")
    elif kind == "text":
        text = node.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        size = node.get("size")
        style.append(f"font-size:{_SIZE_PX.get(size, _SIZE_PX['md']) if isinstance(size, str) else _SIZE_PX['md']}px")
        if node.get("weight") == "bold":
            style.append("font-weight:700")
        color = node.get("color")
        if isinstance(color, str) and _COLOR_RE.fullmatch(color):
            style.append(f"color:{color}")
        body = html.escape(text, quote=True).replace("\r\n", "\n").replace("\n", "<br>")
        out.append(f'<p style="{";".join(style)}">{body}</p>')
    elif kind == "separator":
        out.append("<hr>")


_PAGE = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<meta name="referrer" content="no-referrer">
<meta name="format-detection" content="telephone=no">
<title>{title}</title>
<style>
:root {{ color-scheme: light; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: #f2f2f2; color: #111111; line-height: 1.55;
  font-family: -apple-system, "PingFang TC", "Noto Sans TC", "Microsoft JhengHei", sans-serif;
  -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }}
main {{ max-width: 760px; margin: 0 auto; padding: 12px; }}
.card {{ background: #ffffff; border-radius: 16px; padding: 18px 16px; margin: 0 0 18px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.12); }}
.part + .part {{ margin-top: 18px; }}
.header {{ border-bottom: 2px solid #e6e6e6; padding-bottom: 12px; }}
.col {{ display: flex; flex-direction: column; }}
.row {{ display: flex; flex-direction: row; flex-wrap: wrap; column-gap: 12px; }}
p {{ margin: 0; overflow-wrap: anywhere; }}
hr {{ border: 0; border-top: 2px solid #dddddd; margin: 10px 0; width: 100%; }}
.note {{ text-align: center; color: #555555; font-size: 18px; padding: 4px 16px 28px; margin: 0; }}
</style>
</head>
<body>
<main>
{cards}
</main>
<p class="note">{note}</p>
</body>
</html>
"""


def render_html(card: dict, title: str, *, now: float | None = None, kind: str = DEFAULT) -> str:
    """整張卡（bubble 或 carousel）→ 一頁大字版；carousel 每張一段，錨點 #b1、#b2…。
    頁尾寫產生時間和最多保留多久：一般頁寫天數，健康頁（kind="health"）寫小時數、刪除紀錄或暫停時會清掉。"""
    many = isinstance(card, dict) and card.get("type") == "carousel"
    out: list[str] = []
    for index, bubble in enumerate(_bubbles(card), 1):
        anchor = f' id="b{index}"' if many else ""
        out.append(f'<section class="card"{anchor}>')
        for part in _PARTS:
            box = bubble.get(part)
            if isinstance(box, dict):
                out.append(f'<div class="part {part}">')
                _render_node(box, out)
                out.append("</div>")
        out.append("</section>")
    made = datetime.fromtimestamp(time.time() if now is None else now, _TZ).strftime("%m/%d %H:%M")
    if kind == HEALTH:
        keep = f"連結最多保留 {health_ttl_hours()} 小時，刪除紀錄或暫停記錄時會一起清掉"
    else:
        keep = f"連結最多保留 {-(-ttl_seconds() // 86400)} 天"  # 無條件進位：頁尾寫的是「最多保留」
    note = f"咪寶卡片大字版｜{made} 產生｜{keep}，咪寶重新連線後可能提早失效，打不開就在群組重叫一次卡片"
    return _PAGE.format(
        title=html.escape(title or "咪寶卡片", quote=True),
        cards="\n".join(out),
        note=html.escape(note, quote=True),
    )


# ── 存頁、加按鈕 ──────────────────────────────────────────────────────────────


def _write_private(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=_TMP_PREFIX, suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _prune(
    directory: Path,
    now: float,
    keep: str,
    *,
    ttl: int | None = None,
    max_pages: int | None = None,
    name_re: re.Pattern[str] = _PAGE_NAME_RE,
) -> None:
    """只動這個模組自己的檔：過期的頁、超過上限的最舊頁、放超過一小時的暫存檔。
    ttl／max_pages／name_re 不給就是一般頁（TTL 天數、MAX_PAGES、22 字檔名）；健康頁另給自己的。

    symlink、目錄、檔名不符的一律不碰（health 子目錄是目錄，清一般頁時不會被動到）；剛寫好的那一頁（keep）不刪。
    TODO(2026-10-16 審查延後)：一般頁在啟動時（lifespan）也清一次。現在只在 publish 時清，功能關掉或很久沒人叫卡片時
    過期頁會留在磁碟上（路由照樣回 404）。健康頁另外由每天的健康背景執行緒清（prune_health_pages）。
    """
    cutoff = now - (ttl_seconds() if ttl is None else ttl)
    limit = MAX_PAGES if max_pages is None else max_pages
    pages: list[tuple[float, str]] = []
    with os.scandir(directory) as entries:
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue
                mtime = entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            if _TMP_NAME_RE.fullmatch(entry.name):
                if mtime < now - TMP_MAX_AGE_SEC:
                    _unlink(entry.path)
            elif name_re.fullmatch(entry.name) and entry.name != keep:
                if mtime < cutoff:
                    _unlink(entry.path)
                else:
                    pages.append((mtime, entry.path))
    pages.sort()
    room = limit - 1 if keep else limit  # 剛寫好的那頁也算一頁
    for _mtime, path in pages[: max(0, len(pages) - room)]:
        _unlink(path)


def _family_names() -> set[str]:
    """本機別名檔與角色檔裡的家人名字（含三字名的短名）；爸爸、媽媽這類稱謂不算。

    讀不到別名、或讀出來是空的就丟例外（呼叫端當成不存頁）：`load_user_aliases()` 自己會把讀檔錯誤吞掉回 {}，
    沒有名單的守門等於沒有守門（2026-10-25 GP2 F3），所以「沒名字」一律當作失敗，不當作「沒有人要擋」。"""
    import line_mentions

    names = {str(label or "").strip().lstrip("@").strip() for label in line_mentions.load_user_aliases().values()}
    names |= set(line_mentions.configured_family_alias_mapping(include_short=True))
    found = {name for name in names if len(name) >= 2 and name not in _HEALTH_PAGE_TITLES}
    if not found:
        raise RuntimeError("no family aliases to check the page against")
    return found


def _card_strings(node: Any, depth: int = 0) -> list[str]:
    """卡片上所有的字（text 節點、按鈕的 label／text）；只看字，不看 HTML 的樣式（字型名稱不算）。"""
    found: list[str] = []
    if depth > _MAX_DEPTH:
        return found
    if isinstance(node, dict):
        if node.get("type") == "text" and isinstance(node.get("text"), str):
            found.append(node["text"])
        action = node.get("action")
        if isinstance(action, dict):
            found += [value for key in ("label", "text") if isinstance(value := action.get(key), str)]
        for key, value in node.items():
            if key != "action":
                found += _card_strings(value, depth + 1)
    elif isinstance(node, list):
        for item in node:
            found += _card_strings(item, depth + 1)
    return found


def _names_family(card: Any, title: str) -> bool:
    """健康頁的最後一關（GP2 N3）：卡片上的字（含標題）有家人的別名或「Andrew」。網頁版卡片本來就只寫
    「家人回報」「私訊管理者」；這裡是怕之後改卡片時漏掉。讀不到別名檔就丟例外（呼叫端當成不存頁）。"""
    shown = [title or "", *_card_strings(card)]
    names = _family_names()
    return any("andrew" in text.casefold() or any(name in text for name in names) for text in shown)


def publish(card: dict, title: str, *, kind: str = DEFAULT) -> str | None:
    """存一頁大字版，回頁面網址；關掉、沒有合格的對外網址或目錄、寫檔出錯都回 None（卡片照送、不加按鈕）。

    kind="health"（爸媽健康卡，2026-10-23）：健康頁開關也要開著；頁面上有家人別名或「Andrew」就不存；
    存在 health/ 子目錄、檔名 sha256(token)，用健康頁自己的 TTL 與上限清理。"""
    health = kind == HEALTH
    if kind not in (DEFAULT, HEALTH) or not enabled() or (health and not health_enabled()):
        return None
    base = public_base_url()
    if base is None:
        logger.info("card page skipped reason=no_public_url")
        return None
    try:
        if health and _names_family(card, title):
            logger.warning("card page skipped reason=family_name kind=health")
            return None
        page = render_html(card, title, kind=kind)
        token = secrets.token_urlsafe(16)
        directory = pages_dir()
        try:
            directory.mkdir(mode=0o700)  # 不建上層：state/ 要維持 0700（restart_lock.sh）
        except FileExistsError:
            pass
        if not _dir_ok(directory):
            logger.warning("card page skipped reason=unsafe_dir")
            return None
        name = f"{token}.html"
        if health:
            directory = directory / HEALTH
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            if not _dir_ok(directory):
                logger.warning("card page skipped reason=unsafe_dir kind=health")
                return None
            name = _health_name(token)
        _write_private(directory / name, page)
    except Exception as exc:
        logger.warning("card page not published error_type=%s", type(exc).__name__)
        return None
    url = f"{base}/card/{token}"
    _remember(url)
    try:
        if health:
            _prune(directory, time.time(), name, ttl=health_ttl_seconds(), max_pages=HEALTH_MAX_PAGES,
                   name_re=_HEALTH_NAME_RE)
        else:
            _prune(directory, time.time(), keep=name)
    except Exception as exc:  # 清不掉舊頁不影響這一頁
        logger.warning("card page prune failed error_type=%s", type(exc).__name__)
    return url


def discard(url: str) -> None:
    """剛存好、卻沒有送出去的頁（加了按鈕超過 LINE 限制或驗不過）：刪掉，不留一頁沒人點得到、卻拿到網址就看得到的頁。
    只認這個程序剛發出的網址；不在清單裡的什麼都不做。"""
    try:
        match = _PAGE_URI_RE.fullmatch(url or "")
        if match is None:
            return
        page_url = match.group(1)
        with _MINTED_LOCK:
            if _MINTED.pop(page_url, False) is False:
                return
        token = page_url.rsplit("/", 1)[1]
        directory = pages_dir()
        if not _dir_ok(directory):
            return
        _unlink(str(directory / f"{token}.html"))
        if _dir_ok(directory / HEALTH):
            _unlink(str(directory / HEALTH / _health_name(token)))
    except Exception as exc:
        logger.warning("card page discard failed error_type=%s", type(exc).__name__)


def purge_health_pages() -> int:
    """刪掉所有爸媽健康頁，回刪掉幾頁（刪除紀錄、暫停、收回訊息、緊急停用時呼叫）。

    不看開關（GP1 #10）：功能關掉之後留在磁碟上的頁也要清。只刪 health 子目錄裡檔名合規則的一般檔（和暫存檔）：
    不建目錄、不 rmtree、不跟 symlink（頁面目錄或 health 本身是 symlink、不是自己的都不碰）。失敗只記 warning。"""
    removed = 0
    try:
        parent = os.open(pages_dir(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return 0  # 沒有頁面目錄＝沒有頁
    try:
        try:
            fd = os.open(HEALTH, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        except OSError:
            return 0
        try:
            if os.fstat(fd).st_uid != os.geteuid():
                return 0
            with os.scandir(fd) as entries:
                for entry in entries:
                    page = _HEALTH_NAME_RE.fullmatch(entry.name) is not None
                    if not page and not _TMP_NAME_RE.fullmatch(entry.name):
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        os.unlink(entry.name, dir_fd=fd)
                    except OSError:
                        continue
                    removed += page
        finally:
            os.close(fd)
    except Exception as exc:
        logger.warning("health card pages purge failed error_type=%s", type(exc).__name__)
    finally:
        os.close(parent)
    return removed


def prune_health_pages(now: float | None = None) -> None:
    """每天的健康背景執行緒（每 10 分鐘）呼叫（GP2 N6）：過期與超量的健康頁清掉；健康頁關掉時全部清掉。"""
    try:
        if not health_enabled():
            purge_health_pages()
            return
        directory = pages_dir() / HEALTH
        if not _dir_ok(pages_dir()) or not _dir_ok(directory):
            return  # 還沒有健康頁（或目錄不安全：publish 也不會寫）
        _prune(directory, time.time() if now is None else now, "", ttl=health_ttl_seconds(),
               max_pages=HEALTH_MAX_PAGES, name_re=_HEALTH_NAME_RE)
    except Exception as exc:
        logger.warning("health card pages prune failed error_type=%s", type(exc).__name__)


def page_button(uri: str) -> dict:
    """可以點的 box（Flex 的 button 不能調字級；作法同 flex_menu.build_large_menu）。"""
    return {
        "type": "box",
        "layout": "vertical",
        "margin": "lg",
        "backgroundColor": "#EEEEEE",
        "cornerRadius": "lg",
        "paddingAll": "lg",
        "action": {"type": "uri", "label": BUTTON_LABEL, "uri": uri},
        "contents": [
            {"type": "text", "text": BUTTON_LABEL, "size": "xxl", "weight": "bold", "color": "#111111", "align": "center"}
        ],
    }


def with_page_button(card: dict, url: str) -> dict:
    """回一份新卡：每張 bubble 的 footer 最後加「🔍 看大字版」（carousel 第 i 張連到 #bi）。"""
    paged = copy.deepcopy(card)
    many = paged.get("type") == "carousel"
    for index, bubble in enumerate(_bubbles(paged), 1):
        footer = bubble.get("footer")
        if footer is None:
            footer = bubble["footer"] = {"type": "box", "layout": "vertical", "contents": []}
        if not isinstance(footer, dict) or footer.get("type") != "box" or not isinstance(footer.get("contents"), list):
            continue
        footer["contents"].append(page_button(f"{url}#b{index}" if many else url))
    return paged


# ── 路由 ───────────────────────────────────────────────────────────────────────

# uvicorn 的 access log 會記下 /card/<token>：main 在 import 時給 uvicorn.access 加 filter 遮掉（2026-10-23 A1 #4）。
router = APIRouter()

# 一般頁和健康頁共用（2026-10-23：不再舉股票的例子）
_GONE_PAGE = """<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>連結已失效</title>
<style>body{margin:0;padding:32px 20px;font-family:-apple-system,"PingFang TC","Noto Sans TC",sans-serif;
font-size:24px;line-height:1.6;color:#111111;background:#f2f2f2}</style></head>
<body><p>這個大字版連結已經失效。請在群組再叫一次卡片。</p></body></html>
"""


def _gone() -> HTMLResponse:
    return HTMLResponse(_GONE_PAGE, status_code=404, headers=_HEADERS)


def _read_page(path: Path, ttl: int, *, unlink_expired: bool = False) -> str | None:
    """讀一頁：不是一般檔、太大、過期、讀不了都回 None。unlink_expired：過期的順手刪掉（健康頁，GP1 nit）。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    expired = False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PAGE_BYTES:
            return None
        if time.time() - info.st_mtime > ttl:
            expired = True
            return None
        return os.read(fd, MAX_PAGE_BYTES + 1).decode("utf-8")
    except (OSError, UnicodeError):
        return None
    finally:
        os.close(fd)
        if expired and unlink_expired:
            _unlink(str(path))


@router.get("/card/{token}", include_in_schema=False)
def card_page(token: str) -> HTMLResponse:
    """大字版頁面：token 格式不對、找不到、過期、不是一般檔、目錄不安全、功能關掉，都回同一個 404。
    先找一般頁，再找健康頁（health/sha256(token).html，健康頁開關要開著）；各用各的 TTL。"""
    if not enabled() or not _TOKEN_RE.fullmatch(token or ""):
        return _gone()
    directory = pages_dir()
    if not _dir_ok(directory):
        return _gone()
    body = _read_page(directory / f"{token}.html", ttl_seconds())
    if body is None and health_enabled() and _dir_ok(directory / HEALTH):
        body = _read_page(directory / HEALTH / _health_name(token), health_ttl_seconds(), unlink_expired=True)
    if body is None:
        return _gone()
    return HTMLResponse(body, headers=_HEADERS)
