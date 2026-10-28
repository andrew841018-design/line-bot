"""家族財經觀點追蹤器上線通知（已停用，不再推播）。

這支腳本原本是一次性 ad-hoc 推播，宣傳 /觀點 和每週日的觀點回顧。2026-10-24 財經觀點
拿掉了（/觀點 只回一句固定句，選單沒有按鈕，週報也早就不推），所以再跑這支腳本只會把
一則過時、而且指向已停用指令的訊息送進家人群組。

檔案保留只是為了讓舊的引用不會變成死路徑；沒有排程在叫它（launchd、n8n、jobs 都沒有）。
現在 main() 什麼都不送，也不讀 .env、不連 LINE。
"""

from __future__ import annotations

import sys

RETIRED_NOTICE = "announce_finance_view 已停用（2026-10-24 財經觀點拿掉）：沒有推播任何訊息。"


def main() -> int:
    print(RETIRED_NOTICE)
    return 1


if __name__ == "__main__":
    sys.exit(main())
