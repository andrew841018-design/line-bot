import os
import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
USER_ID = os.getenv("DISCORD_USER_ID")


def send_dm(message: str) -> bool:
    headers = {"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json"}

    # 建立 DM channel
    r = requests.post(
        "https://discord.com/api/v10/users/@me/channels",
        headers=headers,
        json={"recipient_id": USER_ID},
    )
    if r.status_code != 200:
        print(f"建立 DM 失敗: {r.status_code} {r.text}")
        return False

    channel_id = r.json()["id"]

    # 送訊息
    r = requests.post(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        headers=headers,
        json={"content": message},
    )
    if r.status_code != 200:
        print(f"送訊息失敗: {r.status_code} {r.text}")
        return False

    print("Discord DM 送出成功")
    return True


if __name__ == "__main__":
    import sys

    msg = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "測試提醒"
    send_dm(msg)
