#!/usr/bin/env python3
"""一次性 push 9 條 image reply 到 family 群（draft 已準備好）。

跑法：
    cd /Users/andrew/Desktop/andrew/Data_engineer/line_bot
    .venv/bin/python jobs/push_pending_drafts.py

跑完 9 條會 push 出去（quote-reply 原圖），並從 pending 清掉。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from dotenv import load_dotenv
load_dotenv(BASE / ".env")

import pending_store
from line_push_client import LinePushError, line_access_token, push_messages


GROUP = "C83c5609ada4df93fa7f3239c24685133"

DRAFTS: dict[str, str] = {
    "245107": (
        "這張寫得很基本但對\n\n要補一個關鍵\nEPS 不能單看\n"
        "公司增資、發新股\nEPS 會被稀釋\n真正獲利能力要看 ROE\n\n"
        "例如台積電 EPS 50\n配合 ROE 30%\n合起來才完整\n\n"
        "證交所 / mops.twse.com.tw\n都查得到個股資料"
    ),
    "870754": (
        "這張漫畫戳得很準\n兩個轉向都真實\n\n"
        "核能：民進黨從反核到默認重啟\n"
        "- 賴清德核三延役表態鬆動\n"
        "- AI 用電爆炸 缺電壓力倒灌\n"
        "- 半導體產業要 24/7 穩定電力\n"
        "- 風光發電補不上 base load\n\n"
        "政治認同：從台獨到挺中華民國\n"
        "- 賴清德 2024 就職刻意提中華民國憲法\n"
        "- 黨綱沒改 但行動上不動\n"
        "- 川普回任後對台政策硬度回升\n\n"
        "我的觀點：\n不是「漫畫笑」帶過的事\n是真實的政治務實化\n"
        "- 反核派必須面對 AI 用電現實\n"
        "- 獨派必須面對美方紅線\n"
        "- 黨綱口號 ≠ 執政實務\n\n"
        "務實的轉向 = 政治成熟\n笑 10 年青春浪費 有點 cynical 了\n\n"
        "來源：\n- 風傳媒 賴清德核能轉向\n"
        "- 自由時報 2024 就職演說\n"
        "- Reuters 川普一中政策評論"
    ),
    "349725": (
        "kksabis 抓到一個重點\n但結論太簡化\n\n"
        "同意他指出：\n"
        "- 川普「不希望台獨」是紅線\n"
        "- 賴政府文稿用「中華民國」是憲法現實\n"
        "- 大陸委員會留行政院不轉外交部\n"
        "- 確實有「特殊兩岸關係」表述\n\n"
        "不同意「回到馬英九時代」：\n\n"
        "馬英九時代核心是：\n- 九二共識 + ECFA + 國共交流\n\n"
        "賴清德現在是：\n"
        "- 不講九二共識\n- 走「中華民國台灣」雙軌\n"
        "- 對中軍演對峙、無交流\n- 抗中保台基調沒變\n\n"
        "修辭收斂 ≠ 路線翻車\n"
        "這是川普壓力下對美調整\n實質仍對中強硬\n\n"
        "來源：\n- 總統府 2024 就職全文\n"
        "- 風傳媒 川普對台影響\n"
        "- 美國之音 兩岸特殊關係談話"
    ),
    "511781": (
        "這家 CP 值不錯\n雞腿排 120 比附近便當便宜\n鯖魚 145 算合理\n\n"
        "兩個小提醒：\n- 自備餐盒省 5 元 順手做可以\n"
        "- 黑胡椒里肌 110 最便宜\n  但里肌容易柴\n  雞腿排比較不踩雷"
    ),
    "022044": (
        "這張存著很好用\n\n最常用的：\n"
        "- 清潔隊 2391-4913 大物清運\n"
        "- 路燈維修 2594-2744\n"
        "- 派出所 2321-8656\n\n"
        "里長 0919-588497\n有活動會 line 通知\n要的話可以加他群"
    ),
    "428155": (
        "這集講三檔\n都是矽光子/CPO 受惠族群\n\n"
        "聯鈞 (2477)\n- AOC + 光收發模組\n- 客戶含 NVIDIA\n"
        "- 估值已貴 PE 30+\n- 現在追風險高\n\n"
        "前鼎 (4908)\n- 光被動元件\n- AI server 間接受惠\n"
        "- 籌碼分散度比聯鈞好\n\n"
        "有成精密 (4541)\n- 精密金屬加工\n"
        "- 跨足光通訊 connector 殼體\n- 小型股 流動性差\n- 不適合大資金\n\n"
        "提醒：\n理財周刊推時通常是後段\n別 fomo 衝高接刀\n"
        "要分批 + 設停利\n拉回 5 日線再看\n\n"
        "來源：\n- 公開資訊觀測站\n- 工商時報矽光子產業\n- 鉅亨網光通訊整理"
    ),
    "223569": (
        "這份是親身感受不是廣告\n但有些要分清楚\n\n"
        "科學支持較強：\n- 益生菌 排便：strong\n- 膳食纖維 便秘：strong\n"
        "- 葉黃素 黃斑保護：mid\n  飛蚊本身是玻璃體混濁\n  改善有限\n\n"
        "爭議較大：\n- NMN 抗老逆轉：weak\n  動物實驗為主\n  「頭髮黑回來」是個案\n"
        "- 膠原蛋白「臉變白」：weak\n  口服會被消化\n  效果多來自 + 維 C 協同\n"
        "- 魚油「思考倍速」：weak\n  EPA/DHA 對腦部正面但沒這麼神\n\n"
        "務實建議：\n- 維 C、魚油、益生菌、纖維 = 基本盤\n"
        "- NMN、玻尿酸、薑黃 = 加分別當核心\n"
        "- 「神奇感受」先排除安慰劑\n\n"
        "來源：\n- 衛福部食藥署保健食品\n"
        "- 美國 NIH NMN 臨床整理\n- 台灣家醫學會葉黃素文獻"
    ),
    "807082": (
        "這活動可以做但有條件\n\n"
        "划算前提：\n- 你/家人有 uniopen 聯名卡\n"
        "- 房屋稅是「自繳」非銀行扣繳\n- 願意跑 7-11\n\n"
        "OPENPOINT 報酬率：\n- 2000 元繳稅拿 50 點 = 2.5%\n"
        "- 2000 以上拿 100 點 = 約 5%（稅越多 % 越低）\n\n"
        "提醒：\n- 每戶上限 100 點\n- 不能拆單\n"
        "- 信用卡繳稅另有銀行回饋\n\n"
        "郵局/銀行扣繳就別轉\n保留扣繳通常更穩\n\n"
        "來源：\n- 中信銀 uniopen 卡頁\n- 7-11 ibon 繳稅"
    ),
    "595845": (
        "這隻是孟加拉貓嗎\n眼睛綠的好亮\n被你捏臉還沒生氣\n脾氣不錯欸\n\n"
        "下次拍全身\n看背上斑點"
    ),
}


def main() -> int:
    token = line_access_token()
    if not token:
        print("ERROR: no LINE token")
        return 1

    data = pending_store.load()
    items = data.get(GROUP, [])
    if not items:
        print("INFO: pending empty, nothing to push")
        return 0

    pushed = failed = skipped = 0
    for entry in list(items):
        if entry.get("type") != "image":
            skipped += 1
            continue
        mid = entry.get("message_id", "")
        suffix = mid[-6:]
        text = DRAFTS.get(suffix)
        if not text:
            print(f"  ⚠ no draft for suffix {suffix}, skip")
            skipped += 1
            continue
        qt = entry.get("quote_token")

        msg: dict = {"type": "text", "text": text}
        if qt:
            msg["quoteToken"] = qt
        try:
            push_messages(GROUP, [msg], timeout=15, fallback_token=token)
        except LinePushError as e:
            failed += 1
            print(f"  ❌ {suffix} push failed: {str(e)[:200]}")
            continue

        pending_store.remove_by_message_id(GROUP, mid)
        pushed += 1
        print(f"  ✓ {suffix} pushed + removed from pending")

        time.sleep(1.2)

    print()
    print(f"Summary: pushed={pushed} failed={failed} skipped={skipped}")
    print(f"Remaining pending: {len(pending_store.load().get(GROUP, []))} item(s)")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
