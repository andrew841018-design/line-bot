"""LINE bot 自動健康監測（launchd 每 30 分鐘觸發）。

目的：偵測「家人講話 bot 卻沒回」這類隱性故障，能自修就自修，不能就 Discord DM。

省 token 設計：正常運作完全不打 Gemini，只看本地檔案 + DB。
只有「bot 自認 quota 爆」時才探測 lite 一次，確認是不是誤判。

四層比對：
  L1 bot 自認狀態：讀 quota_state.json（最便宜）
  L2 對話比對：近 2 小時內家人發了多少訊息、bot 回了多少（DB 查詢）
  L3 Pending 累積：跟 24 小時前比 pending 筆數成長
  L4 Gemini lite 探測：**僅在 L1 顯示 bot 自認爆時才打**，確認是否誤判

自修觸發條件（嚴格）：
  L1 bot 自認爆 + L4 lite 探測 OK → 清 quota_state.json + 重啟 uvicorn

通知條件（任一，60 分內同類不重複）：
  - 自修完成
  - L2 anomaly：N >= 3 實質訊息（排除閒聊短語）、M = 0 bot 回覆
  - L3 anomaly：pending 24h 內成長 > 20 且沒有任何 drain
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")
sys.path.insert(0, str(BASE))

from notify_discord import send_dm  # noqa: E402

QUOTA_STATE_FILE = BASE / "quota_state.json"
PENDING_FILE = BASE / "pending_explicit_reply.json"
HEALTH_STATE_FILE = BASE / "health_monitor_state.json"
DB_FILE = BASE / "line_bot.db"
GROUP_ID = os.environ.get("LINE_ALLOWED_GROUP_ID") or os.environ.get(
    "ALLOWED_GROUP_ID", ""
)

# 不算「需要回覆」的閒聊短語（跟 burst_filter 的 _CHITCHAT_EXACT 對齊）
CHITCHAT_EXACT = {
    "哈哈", "哈哈哈", "XD", "LOL", "好", "好喔", "好的", "ok", "OK", "Ok",
    "讚", "嗯", "嗯嗯", "晚安", "早安", "午安", "謝謝", "感謝", "Thanks",
    "收到", "了解", "知道了", "辛苦了",
}


def http_health() -> tuple[bool, int]:
    """curl /health，回 (ok, http_code)。pgrep 活但 /health 死 = 殭屍狀態（如 import error）。"""
    try:
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "--interface", "lo0", "--max-time", "5",
             "http://localhost:8080/health"],
            capture_output=True, text=True, timeout=8,
        )
        code = int((r.stdout or "0").strip() or 0)
        return code == 200, code
    except Exception:
        return False, 0


def line_token_check() -> tuple[bool, str]:
    """打 LINE /v2/bot/info 驗 access_token 還有效。不計入月配額。"""
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    if not token:
        return False, "no_token_in_env"
    try:
        import requests
        r = requests.get(
            "https://api.line.me/v2/bot/info",
            headers={"Authorization": f"Bearer {token}"},
            timeout=8,
        )
        if r.status_code == 200:
            return True, ""
        return False, f"HTTP {r.status_code}: {r.text[:100]}"
    except Exception as e:
        return False, str(e)[:100]


def webhook_endpoint_check() -> tuple[bool, str]:
    """打 LINE /v2/bot/channel/webhook/test 端到端探測 webhook 是否能收到事件。
    LINE 會打一個 test event 到目前設定的 webhook URL，若 endpoint 200 才算成功。
    """
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    if not token:
        return False, "no_token_in_env"
    try:
        import requests
        r = requests.post(
            "https://api.line.me/v2/bot/channel/webhook/test",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:120]}"
        body = r.json() if r.content else {}
        # success = true 表示 LINE 成功打到 webhook 並收到 200
        if body.get("success"):
            return True, ""
        return False, f"webhook test 不回 200: {body.get('detail') or body}"
    except Exception as e:
        return False, str(e)[:120]


def sqlite_integrity_check() -> tuple[bool, str]:
    """PRAGMA integrity_check — 偵測 DB 損毀。回 (ok, msg)。"""
    if not DB_FILE.exists():
        return True, ""  # 沒檔不算錯（剛部署）
    try:
        conn = sqlite3.connect(str(DB_FILE), timeout=5)
        cur = conn.cursor()
        cur.execute("PRAGMA integrity_check")
        rows = cur.fetchall()
        conn.close()
        result = (rows[0][0] if rows else "").strip() if rows else ""
        if result == "ok":
            return True, ""
        return False, result[:200]
    except sqlite3.DatabaseError as e:
        return False, f"DatabaseError: {e}"
    except Exception as e:
        return False, str(e)[:120]


def probe_gemini(model: str) -> tuple[bool, str]:
    """打一次最小的 Gemini call，回 (success, error_msg)。"""
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        client.models.generate_content(
            model=model,
            contents="hi",
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        return True, ""
    except Exception as e:
        return False, str(e)[:200]


def read_quota_state() -> dict:
    if not QUOTA_STATE_FILE.exists():
        return {}
    try:
        return json.loads(QUOTA_STATE_FILE.read_text())
    except Exception:
        return {}


def count_recent_activity(hours: int = 2) -> dict:
    """從 raw_messages 統計近 N 小時的家人訊息 vs bot 回覆。"""
    if not DB_FILE.exists() or not GROUP_ID:
        return {"user_msgs": 0, "bot_msgs": 0, "user_substantive": 0}
    since_sec = int(time.time()) - hours * 3600
    try:
        conn = sqlite3.connect(str(DB_FILE))
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, text FROM raw_messages "
            "WHERE group_id = ? AND created_at >= ? ORDER BY created_at",
            (GROUP_ID, since_sec),
        )
        rows = cur.fetchall()
        conn.close()
    except Exception:
        return {"user_msgs": 0, "bot_msgs": 0, "user_substantive": 0, "err": "db"}

    user_msgs = [t for u, t in rows if u != "__bot__"]
    bot_msgs = [t for u, t in rows if u == "__bot__"]
    # 「實質訊息」= 不是純閒聊短語、且字數 >= 5 或含 URL
    substantive = []
    for t in user_msgs:
        s = (t or "").strip()
        if s in CHITCHAT_EXACT:
            continue
        if len(s) >= 5 or "http" in s:
            substantive.append(s)
    return {
        "user_msgs": len(user_msgs),
        "bot_msgs": len(bot_msgs),
        "user_substantive": len(substantive),
    }


def count_pending() -> int:
    if not PENDING_FILE.exists() or not GROUP_ID:
        return 0
    try:
        d = json.loads(PENDING_FILE.read_text())
        return len(d.get(GROUP_ID, []))
    except Exception:
        return -1


def load_health_state() -> dict:
    if not HEALTH_STATE_FILE.exists():
        return {}
    try:
        return json.loads(HEALTH_STATE_FILE.read_text())
    except Exception:
        return {}


def save_health_state(d: dict) -> None:
    try:
        tmp = HEALTH_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2))
        os.replace(tmp, HEALTH_STATE_FILE)
    except Exception:
        pass


def proc_alive(pattern: str) -> bool:
    r = subprocess.run(["pgrep", "-f", pattern], capture_output=True)
    return r.returncode == 0


def restart_uvicorn() -> bool:
    """強制重啟 uvicorn。回 True 表示 /health 200。"""
    try:
        subprocess.run(
            ["pkill", "-f", "uvicorn main:app"], capture_output=True, timeout=10
        )
        time.sleep(2)
        subprocess.Popen(
            [
                str(BASE / ".venv/bin/uvicorn"),
                "main:app",
                "--host", "127.0.0.1",
                "--port", "8080",
            ],
            cwd=str(BASE),
            stdout=open("/tmp/line_bot_health_restart.log", "ab"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        time.sleep(5)
        r = subprocess.run(
            ["curl", "-s", "--interface", "lo0", "http://localhost:8080/health"],
            capture_output=True, text=True, timeout=5,
        )
        return '"status":"ok"' in (r.stdout or "")
    except Exception:
        return False


def restart_cloudflared() -> tuple[bool, str]:
    """重啟 cloudflared tunnel + 抓新 URL + 更新 LINE webhook。回 (success, new_url)。"""
    cf_log = BASE / "cloudflared.log"
    cf_bin = "/Users/andrew/.local/bin/cloudflared"
    if not os.path.exists(cf_bin):
        return False, ""
    try:
        subprocess.run(["pkill", "-f", "cloudflared tunnel"], capture_output=True, timeout=10)
        time.sleep(2)
        cf_log.write_text("")
        subprocess.Popen(
            [cf_bin, "tunnel", "--url", "http://127.0.0.1:8080"],
            stdout=open(cf_log, "ab"), stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        # 等 URL 出現
        import re as _re
        new_url = ""
        for _ in range(30):
            time.sleep(1)
            try:
                m = _re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", cf_log.read_text())
                if m:
                    new_url = m.group(0)
                    break
            except Exception:
                pass
        if not new_url:
            return False, ""
        # 更新 LINE webhook
        token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
        if token:
            import requests
            try:
                requests.put(
                    "https://api.line.me/v2/bot/channel/webhook/endpoint",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={"endpoint": f"{new_url}/callback"},
                    timeout=10,
                )
            except Exception:
                pass
        return True, new_url
    except Exception:
        return False, ""


def attempt_auto_fix() -> bool:
    """清 quota_state.json + 重啟 uvicorn。回 True 表示重啟成功。"""
    try:
        QUOTA_STATE_FILE.write_text(
            json.dumps({"exhausted_until_ts": 0.0, "notified_for_ts": 0.0})
        )
        subprocess.run(
            ["pkill", "-f", "uvicorn main:app"], capture_output=True, timeout=10
        )
        time.sleep(2)
        subprocess.Popen(
            [
                "nohup",
                str(BASE / ".venv/bin/uvicorn"),
                "main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8080",
            ],
            cwd=str(BASE),
            stdout=open("/tmp/line_bot_health_restart.log", "ab"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        time.sleep(4)
        # health 探測
        r = subprocess.run(
            ["curl", "-s", "--interface", "lo0", "http://localhost:8080/health"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return '"status":"ok"' in (r.stdout or "")
    except Exception:
        return False


def main() -> int:
    now = datetime.now()
    state = load_health_state()
    last_alert_ts = float(state.get("last_alert_ts", 0))

    issues_l0: list[str] = []  # 緊急修復項

    # ── L0a uvicorn 進程 + HTTP 200 雙重檢查（pgrep 活但 /health 死 = 殭屍） ─
    uvicorn_up = proc_alive("uvicorn.*main:app")
    health_ok, health_code = (False, 0)
    if uvicorn_up:
        health_ok, health_code = http_health()
        if not health_ok:
            # 進程活但 /health 死 → 殭屍狀態（多半是 import error 或 hung）→ 重啟
            issues_l0.append(
                f"🟡 uvicorn 殭屍：進程在但 /health = HTTP {health_code}（可能 import error）"
            )
            uvicorn_up = False  # 強制走重啟分支

    if not uvicorn_up:
        ok = restart_uvicorn()
        if ok:
            issues_l0.append("✅ uvicorn 死亡 → 自動重啟成功")
            uvicorn_up = True
        else:
            issues_l0.append(
                "🔴 uvicorn 死亡 → 重啟失敗（很可能是 import / syntax error），"
                "請看 /tmp/line_bot_health_restart.log"
            )

    # ── L0b cloudflared 隧道存活 ──────────────────────────────────────────
    cloudflared_up = proc_alive("cloudflared tunnel")
    if not cloudflared_up:
        ok, url = restart_cloudflared()
        if ok and url:
            issues_l0.append(
                f"✅ cloudflared 死亡 → 自動重啟 + LINE webhook 更新為 {url}/callback"
            )
        else:
            issues_l0.append("🔴 cloudflared 死亡 → 重啟失敗（沒抓到新 URL）")

    # ── L0c LINE token 有效性（不計月配額）────────────────────────────────
    token_ok, token_err = line_token_check()
    if not token_ok:
        issues_l0.append(f"🔴 LINE token 失效：{token_err}")

    # ── L0d Webhook 端到端探測（每天最多 1 次，省事件配額）───────────────
    last_webhook_check = float(state.get("last_webhook_check_ts", 0))
    if uvicorn_up and cloudflared_up and token_ok and now.timestamp() - last_webhook_check > 86400:
        wh_ok, wh_err = webhook_endpoint_check()
        state["last_webhook_check_ts"] = now.timestamp()
        if not wh_ok:
            issues_l0.append(f"🔴 Webhook 端到端探測失敗：{wh_err}")

    # ── L0e SQLite integrity_check（每天 1 次，避免每 30 分鎖 DB）─────────
    last_db_check = float(state.get("last_db_check_ts", 0))
    if now.timestamp() - last_db_check > 86400:
        db_ok, db_err = sqlite_integrity_check()
        state["last_db_check_ts"] = now.timestamp()
        if not db_ok:
            issues_l0.append(f"🔴 SQLite 損毀：{db_err}")

    # ── L1 bot 自認狀態（free）────────────────────────────────────────────
    qstate = read_quota_state()
    bot_thinks_exhausted = float(qstate.get("exhausted_until_ts", 0)) > now.timestamp()

    # ── L2 對話比對（近 2 小時，只查本地 DB）────────────────────────────
    activity = count_recent_activity(hours=2)
    silent_anomaly = (
        activity["user_substantive"] >= 3 and activity["bot_msgs"] == 0
    )

    # ── L3 pending 累積（跟 24 小時前比較）────────────────────────────────
    pending_now = count_pending()
    pending_24h_ago = state.get("pending_24h_ago", pending_now)
    pending_24h_at = state.get("pending_24h_at", 0)
    if now.timestamp() - pending_24h_at > 86400:
        # 滾動更新「24 小時前 pending」
        state["pending_24h_ago"] = pending_now
        state["pending_24h_at"] = now.timestamp()
        pending_24h_ago = pending_now
    pending_growth = pending_now - pending_24h_ago

    issues: list[str] = list(issues_l0)
    auto_fixed = bool(issues_l0)
    lite_ok = None  # None = 沒探測；True/False = 探測結果

    # ── L4 lite 探測（僅在 bot 自認爆時打，省 quota）────────────────────
    if bot_thinks_exhausted:
        ok, err = probe_gemini("gemini-2.5-flash-lite")
        lite_ok = ok
        if ok:
            # bot 誤判 → 清狀態 + 重啟
            if attempt_auto_fix():
                auto_fixed = True
                issues.append(
                    "✅ 自修完成：bot 自認 quota 爆但 lite 探測 OK，已清 quota_state.json + 重啟 uvicorn"
                )
            else:
                issues.append(
                    "⚠️ 自修失敗：bot 自認 quota 爆但 lite 探測 OK，自動重啟沒成功，需手動處理"
                )
        else:
            # 真的爆了，只通知不動
            issues.append(
                f"🔴 Gemini lite 也爆了（罕見）：{err[:80]}；等 PT 隔夜重置"
            )

    if silent_anomaly:
        issues.append(
            f"🟡 對話異常：近 2 小時家人發了 {activity['user_substantive']} 則實質訊息，"
            f"bot 一則都沒回（總用戶 {activity['user_msgs']} / bot {activity['bot_msgs']}）"
        )

    if pending_growth > 20:
        issues.append(
            f"🟡 Pending 累積：24 小時內成長 +{pending_growth}（現 {pending_now}），檢查是不是 push 月配額爆"
        )

    print(
        f"[{now.strftime('%Y-%m-%d %H:%M')}] uvicorn={uvicorn_up} cf={cloudflared_up} "
        f"bot_exhausted={bot_thinks_exhausted} "
        f"lite_probe={lite_ok} pending={pending_now} (+{pending_growth}/24h) "
        f"recent: user={activity['user_msgs']} bot={activity['bot_msgs']} substantive={activity['user_substantive']}"
    )
    for i in issues:
        print("  -", i)

    # 通知頻率限制：60 分內同類型不重複通知（除了自修成功一定通知）
    if issues and (auto_fixed or now.timestamp() - last_alert_ts > 3600):
        msg = "🩺 **LINE Bot 健康警示** " + now.strftime("%H:%M") + "\n" + "\n".join(issues)
        # 加診斷上下文
        msg += (
            f"\n\n📊 狀態：bot 自認爆={bot_thinks_exhausted}, "
            f"pending={pending_now}, 近2h 用戶/bot={activity['user_msgs']}/{activity['bot_msgs']}"
        )
        try:
            send_dm(msg)
            state["last_alert_ts"] = now.timestamp()
        except Exception as e:
            print("  ! discord notify failed:", e)

    save_health_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
