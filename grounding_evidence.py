"""Backing for regulation numbers from the pages Gemini's search cited (2026-10-29).

A plain question the crawler missed is answered by Gemini with Google Search
grounding.  Batch 2's B1 keeps a regulation number only if what the program itself
gathered holds it, and Gemini's own segments never count, so these answers always
lost their numbers.  Andrew approved reading the cited pages: a number is said only
when it really appears on one; otherwise the reply stays as it was.

- Only the grounding URLs are fetched, through ``safe_fetch`` (public hosts only,
  every redirect hop checked, bounded bytes and time), with a fixed browser UA and
  nothing else: no cookie, no referer, nothing of the question.
- A body is never handed to a library that unpacks it: compressed and PDF bodies are
  refused by their magic bytes, the text is decoded here and read with one lxml walk
  (trafilatura would gunzip bytes without a limit, REVIEW_gp2_plan_s10 F1–F2).
- What comes back is not page text but the needed quantities found on the pages,
  rendered so that B1's own parser reads each back unchanged (「90天」, 「1000ml」):
  no year and no stray number of a page can reach B1 (REVIEW_gp1_plan_s10 #1).
- Everything runs in daemon threads under one deadline; late work is dropped and
  never holds up a reply or a restart.  Logs carry counts only, never a URL.
"""
from __future__ import annotations

import codecs
import os
import re
import threading
import time
import unicodedata
from collections import Counter
from urllib.parse import urlsplit

import reply_guards
import safe_fetch
import web_scraper

_MAX_PAGES = 4
_MAX_BYTES = 1_000_000
_SCAN_CHARS = 300_000  # of text read per page, counted after NFKC
_PIECE_CHARS = 30_000  # quantities() cuts its input at 40k and may make up a number at the cut
_ACCEPT = frozenset({"text/html", "application/xhtml+xml", "text/plain"})
_UNRESOLVED_HOST = "vertexaisearch.cloud.google.com"  # the grounding redirect itself
# A newline ends these: 「<p>1<span>5公升</span></p>」 stays 15公升, 「<td>1</td><td>5公升</td>」 two lines.
_BLOCKS = frozenset({
    "p", "div", "li", "tr", "td", "th", "br", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul",
    "ol", "section", "article", "header", "footer", "dd", "dt", "blockquote", "pre", "hr", "caption",
    "nav", "aside", "main", "figure", "figcaption", "form", "fieldset", "legend", "address", "details",
    "summary", "dl", "thead", "tbody", "tfoot", "option", "center", "menu",
})
# Charset labels as browsers read them (WHATWG): Taiwan's big5 pages carry cp950 characters.
_LABELS = {
    "big5": "cp950", "x-big5": "cp950", "gb2312": "gbk", "gb_2312-80": "gbk", "x-gbk": "gbk",
    "iso-8859-1": "cp1252", "latin1": "cp1252", "us-ascii": "cp1252", "ascii": "cp1252",
    "shift_jis": "cp932", "sjis": "cp932", "euc-kr": "cp949", "ks_c_5601-1987": "cp949",
}
# Only web encodings: a page may declare any codec Python knows, and some decode in quadratic
# time (punycode: about 40 s for 1 MB, REVIEW_gp2_plan_s10 Phase 6 P6-1).
_WEB_CODECS = frozenset(codecs.lookup(name).name for name in (
    "utf-8", "utf-16", "utf-16-le", "utf-16-be", "cp950", "big5hkscs", "gbk", "gb18030", "cp1252",
    "iso8859-2", "iso8859-3", "iso8859-4", "iso8859-5", "iso8859-6", "iso8859-7", "iso8859-8",
    "iso8859-10", "iso8859-13", "iso8859-14", "iso8859-15", "iso8859-16", "cp1250", "cp1251",
    "cp1253", "cp1254", "cp1255", "cp1256", "cp1257", "cp1258", "cp874", "cp866", "koi8-r", "koi8-u",
    "mac-roman", "cp932", "euc_jp", "iso2022_jp", "cp949",
))
_SKIPPED = frozenset({"script", "style", "noscript", "template", "head"})
_META_CHARSET = re.compile(rb"""<meta[^>]{0,200}?charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]{1,40})""", re.I)
_HEADER_CHARSET = re.compile(r"""charset\s*=\s*["']?([A-Za-z0-9_.:-]{1,40})""", re.I)
_XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]{0,200}\?>")  # lxml refuses a str that still declares one
# Safe places to cut a long line, after NFKC: never inside 「1,000」, 「1.5」 or 「NT$20000」.
_CUT = re.compile(r"[。;!?、\s]|(?<!\d),|,(?!\d)")
STATS_KEYS = (
    "tried", "ok", "non200", "unsupported", "magic", "timeout", "blocked", "unresolved",
    "error", "nomatch", "kept",
)


def enabled() -> bool:
    """The kill switch: LINE_BOT_GROUNDING_BACKING=0 turns the page reads off without a deploy."""
    return os.environ.get("LINE_BOT_GROUNDING_BACKING", "1").strip() != "0"


def needed_quantities(text: str) -> set:
    """The quantities of ``text`` a page may back; years never (B1 treats them apart)."""
    return {q for q in reply_guards.quantities(text or "") if q.kind != "年份"}


def render(quantity) -> str:
    """``quantity`` written so that ``reply_guards.quantities`` reads exactly it back; else ""."""
    text = f"{quantity.value}{'公尺' if quantity.kind == 'm' else quantity.kind}"
    return text if reply_guards.quantities(text) == [quantity] else ""


def format_stats(stats: dict) -> str:
    return " ".join(f"{key}={stats.get(key, 0)}" for key in STATS_KEYS)


def page_backing(urls, needed, *, deadline: float) -> tuple[str, dict]:
    """(backing, counts): the ``needed`` quantities found on the pages at ``urls``, one per line.

    ``deadline`` is a ``time.monotonic()`` value; whatever is not done by then is dropped.
    """
    stats = Counter({key: 0 for key in STATS_KEYS})
    targets = []
    for url in urls or ():
        if isinstance(url, str) and url not in targets and urlsplit(url).scheme in {"http", "https"}:
            targets.append(url)
        if len(targets) == _MAX_PAGES:
            break
    if not targets or not needed:
        return "", dict(stats)
    lock = threading.Lock()
    found: set = set()
    seen_pages: set = set()

    def work(url: str) -> None:
        try:
            outcome, quantities = _read_page(url, set(needed), deadline, seen_pages, lock)
        except Exception:  # noqa: BLE001 - counted, never logged with its message
            outcome, quantities = "error", set()
        with lock:
            stats[outcome] += 1
            found.update(quantities)

    workers = [threading.Thread(target=work, args=(url,), name="grounding-page", daemon=True) for url in targets]
    stats["tried"] = len(workers)
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(max(0.0, deadline - time.monotonic()))
    with lock:  # a late worker may still add to ``found`` or ``stats``: read both once, under the lock
        late = sum(1 for worker in workers if worker.is_alive())
        stats["timeout"] += late
        kept = sorted({text for text in (render(q) for q in found & set(needed)) if text})
        stats["kept"] = len(kept)
        snapshot = dict(stats)
    return "\n".join(kept), snapshot


def _read_page(url: str, needed: set, deadline: float, seen_pages: set, lock) -> tuple[str, set]:
    """(outcome, quantities found) for one cited page; outcome names a ``STATS_KEYS`` counter."""
    left = deadline - time.monotonic()
    if left <= 0.5:
        return "timeout", set()
    try:
        resp = safe_fetch.fetch(
            url, headers=dict(web_scraper._DEFAULT_HEADERS), timeout=min(5.0, left), deadline=left,
            max_bytes=_MAX_BYTES, truncate=True, accept_types=_ACCEPT,
        )
    except safe_fetch.Unsupported:
        return "unsupported", set()
    except safe_fetch.FetchTimeout:
        return "timeout", set()
    except safe_fetch.FetchRefused:
        return "blocked", set()
    except Exception:  # noqa: BLE001 - only its class would be logged, and that says nothing new
        return "error", set()
    if resp.status_code != 200:
        return "non200", set()
    final_host = (urlsplit(getattr(resp, "url", "") or "").hostname or "").lower()
    if final_host == _UNRESOLVED_HOST:
        return "unresolved", set()  # an interstitial, not the cited page
    with lock:
        if resp.url in seen_pages:
            return "ok", set()  # two citations of one page: read once
        seen_pages.add(resp.url)
    body = resp.content or b""
    if _packed(body):
        return "magic", set()
    if time.monotonic() >= deadline:
        return "timeout", set()
    text = _decode(resp, body)
    if "html" in (resp.headers.get("Content-Type", "") or "").lower() or text.lstrip()[:1] == "<":
        text = _html_text(text)
    if time.monotonic() >= deadline:
        return "timeout", set()
    found = _scan(text, needed, deadline)
    return ("ok" if found else "nomatch"), found


def _packed(body: bytes) -> bool:
    """Compressed or PDF bodies, by their magic bytes (zlib by its full header check: deflate,
    a window of 32k or less, no preset dictionary, the checksum)."""
    head = body[:4]
    if head[:2] == b"\x1f\x8b" or head in (b"\x28\xb5\x2f\xfd", b"PK\x03\x04", b"%PDF"):
        return True
    if len(head) >= 2:
        cmf, flg = head[0], head[1]
        if cmf & 0x0F == 8 and cmf >> 4 <= 7 and not flg & 0x20 and ((cmf << 8) | flg) % 31 == 0:
            return True
    return False


def _codec(label: str) -> str:
    """The Python codec for a charset label, if it is a web encoding; else ""."""
    label = (label or "").strip().lower()
    try:
        name = codecs.lookup(_LABELS.get(label, label)).name
    except LookupError:
        return ""
    return name if name in _WEB_CODECS else ""


def _decode(resp, body: bytes) -> str:
    """The page as text: BOM, then a declared charset (header, then <meta>) held even past a bad
    byte, then strict UTF-8 (a cut last character forgiven), a detector, and UTF-8 lossy."""
    if body.startswith(b"\xef\xbb\xbf"):
        return body[3:].decode("utf-8", errors="replace")
    if body[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return body.decode("utf-16", errors="replace")
    header = _HEADER_CHARSET.search(resp.headers.get("Content-Type", "") or "")
    meta = _META_CHARSET.search(body[:2048])
    for label in (header.group(1) if header else "", meta.group(1).decode("ascii", "ignore") if meta else ""):
        codec = _codec(label)
        if codec:
            return body.decode(codec, errors="replace")
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.start >= len(body) - 3:  # the 1 MB cut fell inside the last character
            return body[:exc.start].decode("utf-8")
    try:
        import charset_normalizer

        best = charset_normalizer.from_bytes(body[:65536]).best()
        guessed = _codec(best.encoding) if best is not None else ""
        if guessed not in ("", "cp1252"):  # an ASCII head says nothing about the rest
            return body.decode(guessed, errors="replace")
    except Exception:  # noqa: BLE001 - a lossy decode below still reads the digits
        pass
    return body.decode("utf-8", errors="replace")


def _html_text(html: str) -> str:
    """The visible text of ``html``, a newline at the end of every block, in one linear walk."""
    try:
        from lxml import etree
        from lxml import html as lxml_html

        root = lxml_html.document_fromstring(_XML_DECLARATION.sub("", html, count=1))
    except Exception:  # noqa: BLE001 - an unreadable page backs nothing
        return ""
    parts: list[str] = []
    size = 0
    skipping = 0
    # Comments and processing instructions come as their own events; their tails are page text
    # (「酒類<!-- c -->90天」, Word's <!--StartFragment-->).
    for event, element in etree.iterwalk(root, events=("start", "end", "comment", "pi")):
        if event in ("comment", "pi"):
            if not skipping and element.tail:
                parts.append(element.tail)
                size += len(element.tail)
            continue
        tag = element.tag.lower() if isinstance(element.tag, str) else None
        if event == "start":
            if skipping or tag in _SKIPPED:
                skipping += 1
            elif tag is not None and element.text:
                parts.append(element.text)
                size += len(element.text)
        else:
            if skipping:
                skipping -= 1
                if skipping:
                    continue  # still inside script, style or head: its tails are hidden too
            elif tag in _BLOCKS:
                parts.append("\n")
            if element.tail:
                parts.append(element.tail)
                size += len(element.tail)
        if size > _SCAN_CHARS:
            break
    return "".join(parts)


def _scan(text: str, needed: set, deadline: float) -> set:
    """The ``needed`` quantities in ``text``, read line by line after NFKC in pieces of at most
    30k characters cut at safe places; a piece with no safe cut is dropped, never cut blind."""
    found: set = set()
    budget = _SCAN_CHARS
    for line in text.split("\n"):
        if budget <= 0 or found >= needed or time.monotonic() >= deadline:
            break
        # Cut only at safe places, before NFKC and again after it (it may grow a line 18 times).
        line = _safe_prefix(unicodedata.normalize("NFKC", _safe_prefix(line, budget)), budget)
        budget -= len(line)
        for piece in _pieces(line):
            found.update(q for q in reply_guards.quantities(piece) if q in needed)
    return found


def _safe_prefix(text: str, limit: int) -> str:
    """``text`` if it fits ``limit``; else cut at its last safe place within it, or ""."""
    if len(text) <= limit:
        return text
    cut = 0
    for match in _CUT.finditer(text):  # on the whole text: 「1,|000元」 is no safe place
        if match.end() > limit:
            break
        cut = match.end()
    return text[:cut]


def _pieces(line: str):
    start = 0
    while len(line) - start > _PIECE_CHARS:
        cut = 0
        for match in _CUT.finditer(line, start):
            if match.end() - start > _PIECE_CHARS:
                break
            cut = match.end()
        if not cut:
            return  # no safe place to cut: the rest of the line backs nothing
        yield line[start:cut]
        start = cut
    if start < len(line):
        yield line[start:]
