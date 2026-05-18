"""LINE bot ship-gate preflight 驗證腳本。

在 uvicorn 重啟後 / deploy 前**必跑**：驗 local → cloudflared → LINE 整條 chain
真通，URL drift / DNS propagate 未完成 / handler 殭屍 / Gemini quota 爆等情境
都當場抓出。任何 critical fail → exit non-zero + Discord alert + 結構化 log。

設計來自 2026-05-17 事故：cloudflared URL drift 1.5h webhook drop，user 親眼
看到家人沒回才知，原 `curl --interface lo0 http://127.0.0.1:8080/health` 只驗
localhost、漏了 LINE 端→cloudflared edge→uvicorn 整條 chain。

Exit codes:
  0 = all green
  1 = critical fail (1-9 任一 ✗ 且無法 autofix)
  2 = info fail only (10-11 ✗, 1-9 全 ✓) — bot 仍 deployable
  3 = preflight infra fail (網路斷、requests timeout 自爆)

Usage:
  python preflight_check.py                # 完整跑（有 cache 走 cache）
  python preflight_check.py --force        # skip cache 強制完整跑
  python preflight_check.py --dry-run      # mock LINE API
"""
from __future__ import annotations
import argparse, fcntl, hashlib, json, os, re, sqlite3, subprocess, sys, time
from datetime import datetime
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent
PENDING_PATH = BOT_DIR / "pending_explicit_reply.json"
DB_PATH = BOT_DIR / "line_bot.db"
TUNNEL_URL_STASH = Path("/tmp/cloudflared_line_bot_url.txt")
CLOUDFLARED_LOG = Path.home() / "Library" / "Logs" / "cloudflared_line_bot.log"
LOCK_PATH = Path("/tmp/line_bot_preflight.lock")
CACHE_PATH = Path("/tmp/line_bot_preflight_last_ok.json")
JSONL_LOG = Path.home() / "Library" / "Logs" / "line_bot_preflight.jsonl"
CACHE_TTL_SEC = 300
STASH_MAX_AGE_SEC = 1800
TUNNEL_URL_PATTERN = re.compile(r"^https://[a-z0-9-]+\.trycloudflare\.com$")
UVICORN_PORT = 8080
LOCAL_HEALTH_URL = f"http://127.0.0.1:{UVICORN_PORT}/health"
LOCAL_CALLBACK_URL = f"http://127.0.0.1:{UVICORN_PORT}/callback"
LINE_BOT_INFO = "https://api.line.me/v2/bot/info"
LINE_WEBHOOK_ENDPOINT = "https://api.line.me/v2/bot/channel/webhook/endpoint"
LINE_WEBHOOK_TEST = "https://api.line.me/v2/bot/channel/webhook/test"


class CheckResult:
    __slots__ = ("idx", "name", "status", "detail", "critical")
    def __init__(self, idx, name, critical):
        self.idx = idx; self.name = name; self.status = "skip"
        self.detail = ""; self.critical = critical
    def passed(self): self.status = "pass"; return self
    def failed(self, detail): self.status = "fail"; self.detail = detail; return self
    def autofixed(self, detail): self.status = "autofix"; self.detail = detail; return self
    def skipped(self, detail): self.status = "skip"; self.detail = detail; return self
    @property
    def mark(self):
        return {"pass": "✓", "fail": "✗", "autofix": "⚠", "skip": "·"}[self.status]


def _cloudflared_metrics_port():
    if not CLOUDFLARED_LOG.exists(): return None
    try:
        with open(CLOUDFLARED_LOG, errors="ignore") as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 200_000))
            tail = f.read()
        m = list(re.finditer(r"metrics server on 127\.0\.0\.1:(\d+)", tail))
        if m: return int(m[-1].group(1))
    except OSError: pass
    return None


def _pgrep(pattern):
    r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5)
    return (r.stdout or "").strip().split("\n")[0] or None


def _curl(url, *, timeout=10, method="GET", data=None, headers=None, interface=None):
    cmd = ["curl", "-sS", "-o", "-", "-w", "\n%{http_code}", "--max-time", str(timeout)]
    if interface: cmd += ["--interface", interface]
    if method != "GET": cmd += ["-X", method]
    if data is not None: cmd += ["--data", data]
    for h in headers or []: cmd += ["-H", h]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 3)
        out = r.stdout or ""
        nl = out.rfind("\n")
        if nl == -1: return 0, f"no_status_line: {out[:200]}"
        body = out[:nl]; code = int(out[nl + 1:].strip() or 0)
        return code, body[:500]
    except subprocess.TimeoutExpired: return 0, "curl_timeout"
    except Exception as e: return 0, f"curl_err: {str(e)[:120]}"


def _curl_with_token(url, token, *, timeout=10, method="GET", data=None,
                     extra_headers=None):
    """Token-aware _curl 變體：Authorization 透過 stdin -H @- 不進 argv (防 ps 洩漏)."""
    cmd = ["curl", "-sS", "-o", "-", "-w", "\n%{http_code}", "--max-time", str(timeout),
           "-H", "@-"]
    if method != "GET": cmd += ["-X", method]
    if data is not None: cmd += ["--data", data]
    for h in extra_headers or []: cmd += ["-H", h]
    cmd.append(url)
    auth_header = f"Authorization: Bearer {token}\n"
    try:
        r = subprocess.run(cmd, input=auth_header, capture_output=True, text=True,
                          timeout=timeout + 3)
        out = r.stdout or ""
        nl = out.rfind("\n")
        if nl == -1: return 0, f"no_status_line: {out[:200]}"
        return int(out[nl + 1:].strip() or 0), out[:nl][:500]
    except subprocess.TimeoutExpired: return 0, "curl_timeout"
    except Exception as e: return 0, f"curl_err: {str(e)[:120]}"


def _line_token():
    try:
        sys.path.insert(0, str(BOT_DIR))
        from line_token_refresh import get_line_token
        return get_line_token() or None
    except Exception:
        return os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") or None


def _hash(s): return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def _read_cache():
    if not CACHE_PATH.exists(): return None
    try: d = json.loads(CACHE_PATH.read_text())
    except Exception: return None
    if time.time() - d.get("timestamp", 0) > CACHE_TTL_SEC: return None
    return d


def _write_cache(tunnel_url, webhook_url, exit_code, token=""):
    try:
        CACHE_PATH.write_text(json.dumps({
            "timestamp": time.time(), "tunnel_url": tunnel_url, "webhook_url": webhook_url,
            "tunnel_hash": _hash(tunnel_url), "webhook_hash": _hash(webhook_url),
            "token_hash": _hash(token)[:8] if token else "",
            "exit": exit_code,
        }))
    except OSError: pass


def _cache_still_valid(tunnel_url, webhook_url, token=""):
    c = _read_cache()
    if c is None or c.get("exit") != 0: return False
    if c.get("tunnel_hash") != _hash(tunnel_url): return False
    if c.get("webhook_hash") != _hash(webhook_url): return False
    expected_th = _hash(token)[:8] if token else ""
    if c.get("token_hash", "") != expected_th: return False
    return True


def _emit_jsonl(record):
    try:
        JSONL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(JSONL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError: pass


def _send_discord_alert(message):
    # 15min cooldown by content hash 防 5 wrapper 同分鐘 flood
    dedupe_path = Path("/tmp/preflight_alert_dedupe.json")
    msg_hash = _hash(message)
    now = time.time()
    try:
        cache = json.loads(dedupe_path.read_text()) if dedupe_path.exists() else {}
    except Exception:
        cache = {}
    last = cache.get(msg_hash, 0)
    if now - last < 900:  # 15 min
        return  # silently skip
    cache[msg_hash] = now
    # gc old entries > 1h
    cache = {k: v for k, v in cache.items() if now - v < 3600}
    try:
        dedupe_path.write_text(json.dumps(cache))
    except OSError: pass
    try:
        sys.path.insert(0, str(BOT_DIR))
        from notify_discord import send_dm
        send_dm(message)
    except Exception as e:
        print(f"[preflight] discord alert send failed: {e}", file=sys.stderr)


def check_1_uvicorn_alive():
    r = CheckResult(1, "uvicorn process alive", critical=True)
    return r.passed() if _pgrep("uvicorn main:app") else r.failed("pgrep 沒找到 pid")


def check_2_local_health():
    r = CheckResult(2, "local /health 200", critical=True)
    code, body = _curl(LOCAL_HEALTH_URL, timeout=5, interface="lo0")
    return r.passed() if code == 200 else r.failed(f"http {code} body={body[:120]}")


def check_3_cloudflared_alive():
    r = CheckResult(3, "cloudflared process alive", critical=True)
    return r.passed() if _pgrep("cloudflared.*8080") else r.failed("pgrep 沒找到")


def check_4_stash_valid():
    r = CheckResult(4, "cloudflared URL stash 可讀 + 安全", critical=True)
    if not TUNNEL_URL_STASH.exists():
        return r.failed(f"{TUNNEL_URL_STASH} 不存在"), ""
    try: st = TUNNEL_URL_STASH.stat()
    except OSError as e: return r.failed(f"stat: {e}"), ""
    if st.st_uid != os.getuid():
        return r.failed(f"stash 不屬於當前 uid (owner={st.st_uid}) — 可能被 tamper"), ""
    age = time.time() - st.st_mtime
    url = ""
    for _ in range(2):  # retry 1 次防 cloudflared wrapper non-atomic write 撞期
        try: url = TUNNEL_URL_STASH.read_text().strip()
        except OSError as e: return r.failed(f"read: {e}"), ""
        if url and TUNNEL_URL_PATTERN.match(url): break
        time.sleep(0.5)
    if not url or not TUNNEL_URL_PATTERN.match(url):
        return r.failed(f"URL 非 trycloudflare.com 格式 (retry 後): {url[:80]!r}"), ""
    r.detail = f"url={url} age={int(age)}s"
    return r.passed(), url


def check_4c_double_source(stash_url):
    r = CheckResult(5, "cloudflared metrics 內部 URL 對 stash", critical=True)
    port = _cloudflared_metrics_port()
    if not port: return r.failed("cloudflared metrics port 抓不到")
    code, body = _curl(f"http://127.0.0.1:{port}/metrics", timeout=5)
    if code != 200: return r.failed(f"metrics http {code}")
    m = re.search(r"cloudflared_tunnel_ha_connections\s+(\d+)", body)
    if m:
        n = int(m.group(1))
        if n < 1: return r.failed(f"tunnel ha_connections={n}")
        r.detail = f"ha_connections={n}"; return r.passed()
    r.detail = "metrics 200 但 ha_connections 沒露"; return r.passed()


def check_5_external_tunnel(tunnel_url):
    r = CheckResult(6, f"external {tunnel_url}/health 200", critical=True)
    for i, wait in enumerate([0, 3, 8, 15]):  # 累計 ~26s 覆蓋 trycloudflare edge propagate p99
        if wait: time.sleep(wait)
        code, body = _curl(f"{tunnel_url}/health", timeout=10)
        if code == 200:
            r.detail = f"attempt={i+1}"; return r.passed()
        last = (code, body)
    return r.failed(f"3 retry fail; last http={last[0]} body={last[1][:80]}")


def check_5b_local_callback_route():
    r = CheckResult(7, "/callback no-sig → 400 missing", critical=True)
    code, body = _curl(LOCAL_CALLBACK_URL, method="POST", data="{}",
                       timeout=5, interface="lo0")
    if code == 400 and "missing signature" in body.lower(): return r.passed()
    if code == 500: return r.failed(f"500 ASGI exception; body={body[:120]}")
    return r.failed(f"expected 400 missing, got http {code} body={body[:120]}")


def check_6_line_token():
    r = CheckResult(8, "LINE token /v2/bot/info 200", critical=True)
    tok = _line_token()
    if not tok: return r.failed("no token")
    code, body = _curl_with_token(LINE_BOT_INFO, tok, timeout=8)
    return r.passed() if code == 200 else r.failed(f"http {code} body={body[:120]}")


def check_7_webhook_alignment(tunnel_url, *, dry_run=False):
    r = CheckResult(9, "LINE webhook URL 對齊 cloudflared", critical=True)
    tok = _line_token()
    if not tok: return r.failed("no token"), ""
    expected = f"{tunnel_url}/callback"
    code, body = _curl_with_token(LINE_WEBHOOK_ENDPOINT, tok, timeout=8)
    if code != 200: return r.failed(f"GET http {code}: {body[:120]}"), ""
    try: d = json.loads(body)
    except Exception: return r.failed(f"json parse: {body[:120]}"), ""
    current = d.get("endpoint", "")
    if current == expected: return r.passed(), expected
    if dry_run: return r.autofixed(f"DRY-RUN: would PUT {current!r} → {expected!r}"), expected
    try:
        with open(LOCK_PATH, "w") as lk:
            try:
                # 30s timeout via signal (替 LOCK_NB 避免 morning+health+monitor 撞時直接 fail)
                import signal as _sig
                def _alarm(*_): raise BlockingIOError("flock_timeout")
                _sig.signal(_sig.SIGALRM, _alarm); _sig.alarm(30)
                try:
                    fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
                finally:
                    _sig.alarm(0)
            except BlockingIOError: return r.failed("flock 30s timeout — concurrent run"), expected
            put_code, put_body = _curl_with_token(LINE_WEBHOOK_ENDPOINT, tok, timeout=8,
                method="PUT", data=json.dumps({"endpoint": expected}),
                extra_headers=["Content-Type: application/json"])
            if put_code != 200: return r.failed(f"PUT http {put_code}: {put_body[:120]}"), expected
            time.sleep(1)
            code2, body2 = _curl_with_token(LINE_WEBHOOK_ENDPOINT, tok, timeout=8)
            try: d2 = json.loads(body2)
            except Exception: return r.failed(f"recheck json: {body2[:120]}"), expected
            if d2.get("endpoint") == expected:
                return r.autofixed(f"drift fixed: {current!r} → {expected!r}"), expected
            return r.failed(f"recheck still drift: {d2.get('endpoint')!r}"), expected
    except OSError as e: return r.failed(f"flock: {e}"), expected


def check_8_line_e2e(*, dry_run=False):
    r = CheckResult(10, "LINE → cloudflared → /callback E2E", critical=True)
    if dry_run: return r.skipped("DRY-RUN")
    tok = _line_token()
    if not tok: return r.failed("no token")
    # POST 必須帶 Content-Length（即使 body 空）否則 LINE API 回 411
    code, body = _curl_with_token(LINE_WEBHOOK_TEST, tok, timeout=15, method="POST",
                                  data="", extra_headers=["Content-Type: application/json"])
    if code != 200: return r.failed(f"http {code}: {body[:120]}")
    try: d = json.loads(body)
    except Exception: return r.failed(f"json: {body[:120]}")
    success = d.get("success"); sc = d.get("statusCode")
    sc_ok = sc in (200, "200") or (sc is None and success is True)
    if success is True and sc_ok: return r.passed()
    return r.failed(f"success={success} statusCode={sc!r} reason={d.get('reason')!r}")


def check_9_gemini(model, *, idx, label):
    r = CheckResult(idx, f"Gemini {label} probe", critical=True)
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))
        client.models.generate_content(model=model, contents="hi",
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=0)))
        return r.passed()
    except Exception as e: return r.failed(str(e)[:120])


def check_10_sqlite():
    r = CheckResult(13, "SQLite integrity + WAL checkpoint", critical=False)
    if not DB_PATH.exists(): return r.skipped("DB 不存在")
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        chk = conn.execute("PRAGMA integrity_check").fetchone()
        if chk and chk[0] != "ok":
            conn.close(); return r.failed(f"integrity: {chk[0]!r}")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)"); conn.close()
        return r.passed()
    except Exception as e: return r.failed(str(e)[:120])


def check_11_pending():
    r = CheckResult(14, "pending file JSON load", critical=False)
    if not PENDING_PATH.exists(): return r.skipped("檔不存在")
    try:
        data = json.loads(PENDING_PATH.read_text())
        total = sum(len(v) if isinstance(v, list) else 1 for v in data.values())
        r.detail = f"groups={len(data)} entries={total}"; return r.passed()
    except Exception as e: return r.failed(str(e)[:120])


def run(args):
    start = time.monotonic(); results = []
    def append(r):
        results.append(r)
        print(f"  [{r.mark}] {r.idx:2d}. {r.name}{' — ' + r.detail if r.detail else ''}")
    print("=" * 70)
    print(f"LINE bot preflight @ {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 70)
    append(check_1_uvicorn_alive())
    append(check_2_local_health())
    append(check_3_cloudflared_alive())
    r4, tunnel_url = check_4_stash_valid()
    append(r4)
    if r4.status == "fail":
        return _finalize(results, start, tunnel_url="", webhook_url="", args=args)
    append(check_4c_double_source(tunnel_url))
    append(check_5_external_tunnel(tunnel_url))
    append(check_5b_local_callback_route())
    append(check_6_line_token())
    r7, webhook_url = check_7_webhook_alignment(tunnel_url, dry_run=args.dry_run)
    append(r7)
    if r7.status == "autofix":
        print("  [↻] autofix triggered → re-run external + E2E")
        append(check_5_external_tunnel(tunnel_url))
        append(check_8_line_e2e(dry_run=args.dry_run))
    else:
        append(check_8_line_e2e(dry_run=args.dry_run))
    append(check_9_gemini("gemini-2.5-flash", idx=11, label="main"))
    append(check_9_gemini("gemini-2.5-flash-lite", idx=12, label="lite"))
    append(check_10_sqlite())
    append(check_11_pending())
    return _finalize(results, start, tunnel_url=tunnel_url, webhook_url=webhook_url, args=args)


def _finalize(results, start, *, tunnel_url, webhook_url, args):
    elapsed = time.monotonic() - start
    critical_fail = [r for r in results if r.critical and r.status == "fail"]
    info_fail = [r for r in results if not r.critical and r.status == "fail"]
    autofix = [r for r in results if r.status == "autofix"]
    critical_total = sum(1 for r in results if r.critical)
    info_total = sum(1 for r in results if not r.critical)
    # GP1 C2: Gemini main + lite 都掛才 critical；單一掛降 info
    gemini_fails = [r for r in critical_fail if r.name.startswith("Gemini")]
    if gemini_fails and len(gemini_fails) < 2:
        critical_fail = [r for r in critical_fail if not r.name.startswith("Gemini")]
    if critical_fail: exit_code, verdict = 1, "FAIL"
    elif info_fail: exit_code, verdict = 2, "PASS-WITH-INFO-FAIL"
    else: exit_code, verdict = 0, "PASS"
    cpass = critical_total - len(critical_fail)
    ipass = info_total - len(info_fail)
    summary = f"PREFLIGHT [{verdict}] critical={cpass}/{critical_total} info={ipass}/{info_total} elapsed={elapsed:.1f}s"
    if autofix: summary += f" autofix={len(autofix)}"
    print("-" * 70); print(summary)
    if critical_fail:
        print("Critical fails:")
        for r in critical_fail: print(f"  ✗ {r.name}: {r.detail}")
    if info_fail:
        print("Info fails:")
        for r in info_fail: print(f"  · {r.name}: {r.detail}")
    if exit_code == 0 and tunnel_url and webhook_url:
        _write_cache(tunnel_url, webhook_url, exit_code, token=_line_token() or "")
    _emit_jsonl({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "verdict": verdict, "exit": exit_code, "elapsed": elapsed,
        "tunnel_url": tunnel_url, "webhook_url": webhook_url,
        "results": [{"idx": r.idx, "name": r.name, "status": r.status,
                     "critical": r.critical, "detail": r.detail} for r in results],
    })
    if exit_code != 0 or autofix:
        msg = f"🛫 **LINE bot preflight {verdict}**\n{summary}"
        if critical_fail:
            msg += "\n\nCritical fails:\n" + "\n".join(f"• {r.name}: {r.detail}" for r in critical_fail)
        if autofix:
            msg += "\n\nAutofix:\n" + "\n".join(f"• {r.name}: {r.detail}" for r in autofix)
        _send_discord_alert(msg)
    return exit_code


def main():
    ap = argparse.ArgumentParser(description="LINE bot ship-gate preflight")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.force:
        try:
            url = TUNNEL_URL_STASH.read_text().strip() if TUNNEL_URL_STASH.exists() else ""
            tok = _line_token(); webhook = ""
            if url and tok:
                code, body = _curl_with_token(LINE_WEBHOOK_ENDPOINT, tok, timeout=5)
                if code == 200: webhook = json.loads(body).get("endpoint", "")
            if url and webhook and _cache_still_valid(url, webhook, token=tok or ""):
                print(f"[preflight] cache HIT — skip rerun"); return 0
        except Exception as e:
            print(f"[preflight] cache check failed (continuing): {e}", file=sys.stderr)
    try: return run(args)
    except KeyboardInterrupt: return 130
    except Exception as e:
        print(f"[preflight] infra fail: {e}", file=sys.stderr)
        _emit_jsonl({"ts": datetime.now().isoformat(timespec="seconds"),
                     "verdict": "INFRA-FAIL", "exit": 3, "error": str(e)[:300]})
        return 3


if __name__ == "__main__":
    sys.exit(main())
