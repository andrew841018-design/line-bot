"""LINE Channel Access Token v3 (stateless) 自動換發。

設計：
- v3 stateless token 15 分鐘到期；用 channel_id + channel_secret 隨時換新
- 結果寫到 line_token_cache.json（被 .gitignore），不污染 .env
- token_helper.get_line_token() 是統一入口，過期前 60 秒自動 refresh
- launchd 也每 10 分鐘主動跑一次當保險
- 沒設 LINE_CHANNEL_ID 就 fallback 用 .env 的 long-lived token（向後相容）

使用：
- main.py / 推播 script 改用 token_helper.get_line_token() 而非直接讀 settings
- 一次性設定：在 LINE Developer Console > Basic settings 複製 Channel ID（純數字），
  加到 .env：`LINE_CHANNEL_ID=1234567890`
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CACHE_FILE = BASE / "line_token_cache.json"
TOKEN_ENDPOINT = "https://api.line.me/oauth2/v3/token"
EXPIRY_BUFFER_SEC = 60  # 提前 60 秒視為過期


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return {}


def _save_cache(d: dict) -> None:
    """原子寫入。"""
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2))
    os.replace(tmp, CACHE_FILE)


def refresh_token() -> tuple[bool, str]:
    """打 LINE OAuth v3，拿新 short-lived token 寫進 cache。回 (success, msg)。"""
    channel_id = os.environ.get("LINE_CHANNEL_ID", "").strip()
    channel_secret = os.environ.get("LINE_CHANNEL_SECRET", "").strip()
    if not channel_id:
        return False, "LINE_CHANNEL_ID not set in .env"
    if not channel_secret:
        return False, "LINE_CHANNEL_SECRET not set in .env"
    try:
        r = requests.post(
            TOKEN_ENDPOINT,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "client_id": channel_id,
                "client_secret": channel_secret,
            },
            timeout=10,
        )
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        data = r.json()
        token = data.get("access_token")
        expires_in = int(data.get("expires_in", 900))
        if not token:
            return False, f"no access_token in response: {r.text[:200]}"
        cache = {
            "access_token": token,
            "expires_at": int(time.time()) + expires_in,
            "refreshed_at": int(time.time()),
            "expires_in": expires_in,
            "source": "v3_stateless",
        }
        _save_cache(cache)
        logger.info(
            "refreshed v3 stateless token, expires_in=%ds, expires_at=%s",
            expires_in,
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cache["expires_at"])),
        )
        return True, f"expires_in={expires_in}s"
    except Exception as e:
        return False, str(e)[:200]


def get_line_token() -> str:
    """取得目前可用 LINE token：優先 cache，過期就 refresh，最後 fallback 到 .env long-lived。"""
    cache = _load_cache()
    expires_at = int(cache.get("expires_at", 0))
    if cache.get("access_token") and time.time() < expires_at - EXPIRY_BUFFER_SEC:
        return cache["access_token"]
    # 嘗試 refresh
    ok, _ = refresh_token()
    if ok:
        cache = _load_cache()
        if cache.get("access_token"):
            return cache["access_token"]
    # fallback：env 的 long-lived token（沒設 channel_id 時走這條）
    return os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")


def main() -> int:
    """launchd 進入點：每 10 分鐘跑一次主動 refresh。"""
    ok, msg = refresh_token()
    if ok:
        print(f"[OK] {msg}")
        return 0
    print(f"[SKIP/ERR] {msg}")
    # channel_id 沒設不算錯（向後相容於 long-lived token）
    return 0 if "LINE_CHANNEL_ID not set" in msg else 1


if __name__ == "__main__":
    sys.exit(main())
