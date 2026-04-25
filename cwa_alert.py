"""氣象警示推播 — launchd 每小時觸發。

資料來源（完全免費，無需 API key）：
- 地震：USGS GeoJSON API，篩選台灣周邊 M3.0+
- 颱風：爬 CWA 颱風資訊頁面
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
sys.path.insert(0, str(Path(__file__).parent))

import requests
from bs4 import BeautifulSoup

GROUP_ID = os.environ.get("LINE_ALLOWED_GROUP_ID") or os.environ.get(
    "ALLOWED_GROUP_ID", ""
)
TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")

_PUSH_URL = "https://api.line.me/v2/bot/message/push"
_STATE_FILE = Path(__file__).parent / "alert_state.json"

# 台灣周邊地理範圍
_TW_LAT_MIN, _TW_LAT_MAX = 21.5, 26.0
_TW_LON_MIN, _TW_LON_MAX = 118.0, 123.0
_EQ_MIN_MAG = 3.0  # 只推 M3.0 以上

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def _load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text())
    except Exception:
        return {"pushed_ids": []}


def _save_state(state: dict) -> None:
    _STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))


def _push(text: str) -> None:
    requests.post(
        _PUSH_URL,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
        json={"to": GROUP_ID, "messages": [{"type": "text", "text": text[:5000]}]},
        timeout=10,
    )


def _fetch_earthquakes() -> list[dict]:
    """USGS GeoJSON — 過去 24h M2.5+，篩台灣周邊。"""
    url = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        features = resp.json().get("features", [])
    except Exception as e:
        print(f"USGS fetch failed: {e}")
        return []

    alerts = []
    for f in features:
        props = f.get("properties", {})
        coords = (f.get("geometry") or {}).get("coordinates", [])
        if len(coords) < 3:
            continue
        lon, lat, depth = coords[0], coords[1], coords[2]
        if not (
            _TW_LAT_MIN <= lat <= _TW_LAT_MAX and _TW_LON_MIN <= lon <= _TW_LON_MAX
        ):
            continue
        mag = props.get("mag", 0) or 0
        if mag < _EQ_MIN_MAG:
            continue
        place = props.get("place", "台灣附近")
        eq_id = f.get("id", "")
        t = time.strftime(
            "%Y-%m-%d %H:%M", time.gmtime((props.get("time") or 0) / 1000)
        )
        text = (
            f"🌍 台灣有感地震\n"
            f"規模 M{mag:.1f}，深度 {abs(depth):.0f} 公里\n"
            f"震央：{place}\n"
            f"時間：{t} UTC"
        )
        alerts.append({"id": eq_id, "text": text})
    return alerts


def _fetch_typhoon() -> list[dict]:
    """爬 CWA 颱風動態頁，有颱風才回傳。"""
    url = "https://www.cwa.gov.tw/V8/C/W/OBS/TYPHOON/TYW01.html"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=10, verify=False)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        # 找颱風名稱與強度（頁面結構：<h2> 或 <div class="ty-name">）
        text_content = soup.get_text(separator="\n", strip=True)
        # 有颱風才有「颱風」字樣出現在主要資訊區
        if "颱風" not in text_content[:3000]:
            return []
        # 取前 500 字當摘要
        summary = text_content[:500].strip()
        return [
            {
                "id": f"typhoon_{time.strftime('%Y%m%d%H')}",
                "text": f"🌀 颱風動態\n{summary}",
            }
        ]
    except Exception as e:
        print(f"颱風頁面抓取失敗: {e}")
        return []


def main() -> None:
    if not GROUP_ID or not TOKEN:
        print("ERR: LINE_ALLOWED_GROUP_ID or LINE_CHANNEL_ACCESS_TOKEN not set")
        return

    state = _load_state()
    pushed_ids: list[str] = state.get("pushed_ids", [])
    new_pushed: list[str] = []

    all_alerts = _fetch_earthquakes() + _fetch_typhoon()

    for alert in all_alerts:
        aid = alert["id"]
        if aid in pushed_ids:
            continue
        _push(alert["text"])
        new_pushed.append(aid)
        print(f"推播警示：{alert['text'][:60]}")
        time.sleep(0.5)

    if new_pushed:
        all_ids = (pushed_ids + new_pushed)[-300:]
        _save_state({"pushed_ids": all_ids})
    else:
        print("無新警示")


if __name__ == "__main__":
    main()
