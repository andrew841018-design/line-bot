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
  TODO(2026-10-17 審查延後)：檔名就是 token；要讓大小寫也算數、檔名不等於憑證，改存 sha256(token) 當檔名。
- 不 import main（main 會 import 這裡；import 失敗時 main 不掛路由、不加按鈕、拒收 uri）。
- 只有股票卡、FCN 評估卡做網頁（2026-10-17，main._reply_command_card 的 page=True 加 altText 正面表列）；
  爸媽健康卡、FCN 確認卡不做。
"""

from __future__ import annotations

import copy
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
TUNNEL_URL_FILE = "/tmp/cloudflared_line_bot_url.txt"
DEFAULT_TTL_DAYS = 7
MAX_PAGES = 500
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


def ttl_seconds() -> int:
    try:
        days = float(os.environ.get(TTL_ENV, "").strip() or DEFAULT_TTL_DAYS)
    except ValueError:
        days = DEFAULT_TTL_DAYS
    if days != days:  # NaN
        days = DEFAULT_TTL_DAYS
    return int(min(max(days, 1.0), 30.0) * 86400)


def pages_dir() -> Path:
    raw = os.environ.get(DIR_ENV, "").strip()
    if raw:  # 轉成絕對路徑但不解 symlink：目錄本身是 symlink 就要被 _dir_ok 擋下
        return Path(os.path.abspath(os.path.expanduser(raw)))
    return Path(__file__).resolve().with_name("state") / "card_pages"


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


def render_html(card: dict, title: str, *, now: float | None = None) -> str:
    """整張卡（bubble 或 carousel）→ 一頁大字版；carousel 每張一段，錨點 #b1、#b2…。"""
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
    days = -(-ttl_seconds() // 86400)  # 無條件進位：頁尾寫的是「最多保留」
    note = f"咪寶卡片大字版｜{made} 產生｜連結最多保留 {days} 天，咪寶重新連線後可能提早失效，打不開就在群組重叫一次卡片"
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


def _prune(directory: Path, now: float, keep: str) -> None:
    """只動這個模組自己的檔：過期的頁、超過 MAX_PAGES 的最舊頁、放超過一小時的暫存檔。

    symlink、目錄、檔名不符的一律不碰；剛寫好的那一頁（keep）不刪。
    TODO(2026-10-16 審查延後)：啟動時（lifespan）也清一次。現在只在 publish 時清，功能關掉或很久沒人叫卡片時
    過期頁會留在磁碟上（路由照樣回 404）。
    """
    cutoff = now - ttl_seconds()
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
            elif _PAGE_NAME_RE.fullmatch(entry.name) and entry.name != keep:
                if mtime < cutoff:
                    _unlink(entry.path)
                else:
                    pages.append((mtime, entry.path))
    pages.sort()
    for _mtime, path in pages[: max(0, len(pages) - (MAX_PAGES - 1))]:
        _unlink(path)


def publish(card: dict, title: str) -> str | None:
    """存一頁大字版，回頁面網址；關掉、沒有合格的對外網址或目錄、寫檔出錯都回 None（卡片照送、不加按鈕）。"""
    if not enabled():
        return None
    base = public_base_url()
    if base is None:
        logger.info("card page skipped reason=no_public_url")
        return None
    try:
        page = render_html(card, title)
        token = secrets.token_urlsafe(16)
        directory = pages_dir()
        try:
            directory.mkdir(mode=0o700)  # 不建上層：state/ 要維持 0700（restart_lock.sh）
        except FileExistsError:
            pass
        if not _dir_ok(directory):
            logger.warning("card page skipped reason=unsafe_dir")
            return None
        _write_private(directory / f"{token}.html", page)
    except Exception as exc:
        logger.warning("card page not published error_type=%s", type(exc).__name__)
        return None
    url = f"{base}/card/{token}"
    _remember(url)
    try:
        _prune(directory, time.time(), keep=f"{token}.html")
    except Exception as exc:  # 清不掉舊頁不影響這一頁
        logger.warning("card page prune failed error_type=%s", type(exc).__name__)
    return url


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

# TODO(2026-10-16 審查延後)：uvicorn 的 access log（stdout 的 log 檔，0644）會記下 /card/<token>。風險低：
# 內容本來就公開，token 過 TTL 就失效；要遮的話加一個 logging.Filter。
router = APIRouter()

_GONE_PAGE = """<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>連結已失效</title>
<style>body{margin:0;padding:32px 20px;font-family:-apple-system,"PingFang TC","Noto Sans TC",sans-serif;
font-size:24px;line-height:1.6;color:#111111;background:#f2f2f2}</style></head>
<body><p>這個大字版連結已經失效。請在群組再叫一次卡片（例如再按一次「📈 股票推薦」）。</p></body></html>
"""


def _gone() -> HTMLResponse:
    return HTMLResponse(_GONE_PAGE, status_code=404, headers=_HEADERS)


@router.get("/card/{token}", include_in_schema=False)
def card_page(token: str) -> HTMLResponse:
    """大字版頁面：token 格式不對、找不到、過期、不是一般檔、目錄不安全、功能關掉，都回同一個 404。"""
    if not enabled() or not _TOKEN_RE.fullmatch(token or ""):
        return _gone()
    directory = pages_dir()
    if not _dir_ok(directory):
        return _gone()
    try:
        fd = os.open(directory / f"{token}.html", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return _gone()
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > MAX_PAGE_BYTES
            or time.time() - info.st_mtime > ttl_seconds()
        ):
            return _gone()
        body = os.read(fd, MAX_PAGE_BYTES + 1).decode("utf-8")
    except (OSError, UnicodeError):
        return _gone()
    finally:
        os.close(fd)
    return HTMLResponse(body, headers=_HEADERS)
