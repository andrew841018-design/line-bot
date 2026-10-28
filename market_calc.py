"""市場盤勢（/盤勢，2026-10-17）的純計算：job 與 bot 共用的公式、門檻、交易日曆、新鮮度、固定句與快照欄位表。

Andrew 2026-10-17 原話：「美股/台股的籌碼/基本面/技術面/消息面/期權市場的下注（最大未平倉價位等等，
gamma squzze…)/持股跌破20MA,60MA,120MA,240MA的家數/成交量(微縮or爆大量…我要知道，不能只有量，
我要知道當前量是多還是少）/持股上漲家數與下跌家數比例/13F相關資訊/市場上槓桿的程度/外資動向」
「我想要知道目前市場的狀況」。10/17 的選擇：按了才回、一次全做、只描述狀態不寫買賣、顏色只用藍橘灰、
每一面各自標狀態、不算加權總分。

這裡只放「程式算」的部分（規格：設計文件 PLAN.md v6.2 的 §4、§5、
§18、§19；介面：INTERFACES.md §4、§5、§8）：
- 均線、距離、RSI14（Wilder）、240 日高點回撤；寬度（N+5 個預期交易日都有收盤、未碰到隔離區間才算有效；
  5 日前用兩天都有效的同一批、交集 <80% 不比；狀態由覆蓋率決定）；漲跌家數；240 日新高新低。
- 量能（倍數＝當天量／前 20 個交易日平均、近 250 日百分位、五級標籤、價量四種組合、結算日／早收日旗標）。
- 期權（OI 有效／gamma 有效兩個集合、IV=0 的價內合約改用同履約價價外 IV、月選規則、牆、最大痛點、
  PCR、假設部位的 GEX(S) 曲線與變號點、覆蓋率）；台指選擇權的牆與最大痛點。
- 台股還原除權息（只乘在除權息日之前）、本益比（市值 ÷ 近四季普通股淨利）、股別合併、13F 摘要。
- 矛盾（7 條、同資料日才比）與一句話（固定句型）。
- 交易日曆（休市表由呼叫端傳入）與 `stale_after`（§5 類型表＋§18.1、§18.9 的修正）。
- 外部字串清洗 `clean_external`（§19.1）、`ParseError`（sources 共用）、快照列 id 與 values 欄位表 `ROW_IDS`。

約定：
- 純函式：不讀寫檔案、不連網、不 import 專案模組；所有跟時間有關的函式都要呼叫端傳 `now`。
- numpy 只在算 GEX 時才 import（§19.6：bot 端 market_dashboard 會 import 本模組拿 ROW_IDS，
  它不可以載入 numpy／pandas／matplotlib）。
- sources 的 dataclass（OptionContract、TxoRow、TwBar、ExRight、Quarter、Holding…）用屬性名讀
  （鴨子型別），本模組不 import sources，避免循環相依。
"""

from __future__ import annotations

import calendar as _calendar
import html
import math
import re
import statistics
import unicodedata
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, is_dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Context, Decimal, InvalidOperation, localcontext
from functools import cached_property
from typing import Any, Callable, NamedTuple
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
TPE = ZoneInfo("Asia/Taipei")
UTC = timezone.utc

# ── 門檻（都是起始值，跑一陣子再調） ───────────────────────────────────────────
MA_WINDOWS = (20, 60, 120, 240)
BREADTH_EXTRA = 5  # 寬度的窗口＝N+5 個預期交易日（含 5 日前那天的均線）
COVERAGE_OK = 0.80  # 覆蓋率 ≥80% 才算正常、才比 5 日前
RSI_PERIOD = 14
# RSI 往回至少要有幾筆連續的收盤才算（2026-10-28 審查 seat3 I-1）：Wilder 的種子（前 14 個變化的簡單平均）要靠後面的平滑才被洗掉，
# 缺口後 15–60 筆的 RSI 沒收斂（合成資料實測：完整序列 41.0、缺口後 20 筆算出 56.9，隨機漫步最大誤差 24 點）；80 筆時最大誤差 0.26，
# 卡上印整數不會變。tail 在缺格或碰到隔離區間處截斷，所以缺口後 80 個預期交易日內都沒有 RSI（寧可暫缺，不印不收斂的值）。
RSI_MIN_CLOSES = 80
DRAWDOWN_WINDOW = 240
HIGHLOW_WINDOW = 240
VOL_AVG_DAYS = 20
VOL_PCT_DAYS = 250
VOL_STRONG_SHRINK = 0.5
VOL_SHRINK = 0.8
VOL_EXPAND = 1.5  # 沿用出場警示的放量門檻
VOL_SURGE = 2.5
PV_SPLIT = 1.0  # 價量組合：倍數 ≥1.0 算量增

OPTION_ROOTS = ("QQQ", "SPY", "SOXX")
OPTION_SYMBOL_RE = re.compile(r"^(QQQ|SPY|SOXX)(\d{6})([CP])(\d{8})$")
OPTION_WINDOW_DAYS = 45  # 牆、PCR、GEX 只看 D 之後 45 天內到期
WALL_MIN_CONTRACTS = 100
NEAR_BAND = 0.10  # 「現價 ±10% 兩邊都有」與覆蓋率的範圍
WALL_BAND = 0.15  # 買權牆 [S, 1.15S]、賣權牆 [0.85S, S]（§18.13）
MONTHLY_MIN_CONTRACTS = 40
MONTHLY_MIN_STRIKES = 15
GEX_GRID_LOW, GEX_GRID_HIGH, GEX_GRID_POINTS = 0.85, 1.15, 61  # 每 0.5% 一點
GEX_NEAR_ZERO = 0.02
GEX_COVERAGE_OK = 0.80
DEFAULT_RATE = 0.04  # 3M 公債抓不到時的假設利率（卡上要標「假設利率」）
CONTRACT_MULTIPLIER = 100

STALE_SESSIONS = 4
OPTIONS_GRACE = timedelta(hours=3)
CBOE_MAX_AGE = timedelta(hours=12)
BTC_TTL = timedelta(hours=6)
PREDICTION_TTL = timedelta(hours=6)
NEWS_TTL = timedelta(hours=36)
TRUMP_TTL = timedelta(hours=48)
EVENT_GRACE = timedelta(hours=2)
# 事件清單一定要有的種類，依序保證（2026-10-18：財報季時二十幾檔前十大的財報日會把 FOMC 擠出清單）
EVENT_MUST_KEEP = ("fomc", "cpi", "nfp")
# 同一時刻的事件：必留種類排前面（順序同 EVENT_MUST_KEEP），其他種類排後；publish 與 bot 的排序共用這份
EVENT_PRIORITY = {kind: index for index, kind in enumerate(EVENT_MUST_KEEP)}
EVENT_PRIORITY_OTHER = len(EVENT_MUST_KEEP)


def keep_events(items: Sequence[Any], limit: int, kind_of: Callable[[Any], str | None]) -> list[Any]:
    """items 已依時間排：取前 limit 筆，但 EVENT_MUST_KEEP 各種類最近的一筆一定在內——放不下就擠掉最晚的
    一筆其他事件；回傳仍依原本的時間順序。"""
    if limit <= 0:
        return []
    chosen = list(range(min(limit, len(items))))
    for kind in EVENT_MUST_KEEP:
        if any(kind_of(items[i]) == kind for i in chosen):
            continue
        extra = next((i for i in range(limit, len(items)) if kind_of(items[i]) == kind), None)
        victims = [i for i in chosen if kind_of(items[i]) not in EVENT_MUST_KEEP]
        if extra is not None and victims:
            chosen.remove(max(victims))
            chosen.append(extra)
    return [items[i] for i in sorted(chosen)]
CFTC_DAYS = 16
MONTH_END_DAYS = 62  # FINRA、台股月營收：以月底為錨
MONEYDJ_DAYS = 62
F13_DAYS = 155
FUND_DAYS = 160
FUND_NOT_UPDATED_DAYS = 151  # 季度結束＋下一季（約 91 天）＋申報期限（10-K 60 天）
CALENDAR_FALLBACK_DAYS = 6  # 日曆表外的退路（§18.3）
US_CLOSE_DELAY = timedelta(minutes=30)  # D＝美東已過收盤＋30 分鐘的預期交易日
TW_CLOSE_DELAY = timedelta(hours=1)  # T＝已過 14:30 的預期交易日

# 外部字串長度上限（§10）
MAX_TITLE, MAX_NAME, MAX_TRUMP_EXCERPT = 40, 24, 100

# 顏色只用藍、橘、灰（/股票 的紅漲綠跌顧慮）
COLOR_BLUE, COLOR_ORANGE, COLOR_GREY, COLOR_GREY_LIGHT = "#1E5AA8", "#B7791F", "#555555", "#666666"
COLORS = (COLOR_BLUE, COLOR_ORANGE, COLOR_GREY, COLOR_GREY_LIGHT)

# 量能標籤與旗標
VOL_LABEL_STRONG_SHRINK, VOL_LABEL_SHRINK, VOL_LABEL_NORMAL = "明顯量縮", "量縮", "正常"
VOL_LABEL_EXPAND, VOL_LABEL_SURGE = "放量", "爆量"
VOL_LABELS = (VOL_LABEL_STRONG_SHRINK, VOL_LABEL_SHRINK, VOL_LABEL_NORMAL, VOL_LABEL_EXPAND, VOL_LABEL_SURGE)
PV_LABELS = ("價漲量增", "價漲量縮", "價跌量增", "價跌量縮")
FLAG_SETTLEMENT, FLAG_EARLY_CLOSE = "結算日", "早收日"

# 快照列的狀態與期權狀態
STATUS_OK, STATUS_LOW_COVERAGE, STATUS_INSUFFICIENT = "ok", "low_coverage", "insufficient"
ROW_STATES = ("ok", "unknown_period", "never")
SNAPSHOT_SCHEMA = 1

# ── 固定句（卡片與 altText 只用這些＋程式算的數字與日期；不寫年份，避開 validator 的年份規則） ─────
TEXT_DISCLAIMER = "程式用公開資料算的，只描述狀態，不是買賣建議"  # 禁用字檢查的精確例外
# 摘要輪播的 altText 出錯時退回的固定句（批 1 審查 F2-11）：開頭一樣是「盤勢：」，不放任何數字
TEXT_SUMMARY_ALT_FALLBACK = "盤勢：摘要卡片，往左滑看全部"
TEXT_PREPARING = "盤勢：資料準備中，請過幾分鐘再按一次"
TEXT_BOOTSTRAP = "歷史資料補到 {done}/{total} 檔"
TEXT_DISABLED = "盤勢：暫停使用"
TEXT_STALE = "過期（資料日 {md}）"
TEXT_MISSING = "暫缺"
TEXT_UNKNOWN_PERIOD = "期間未標"
TEXT_INSUFFICIENT = "資料不足"
TEXT_DATE_MISSING = "日期暫缺"  # 0050 細節卡的融資那一列沒有資料日時代替日期（批 1 審查 F2-6；實際上 job 都會寫，這是型別壞掉的退路）
# 美股 ETF 列的收盤是 CBOE 的暫定值（Nasdaq 正式日線還沒到；2026-10-23 審查 r9 Codex 席 I2）：publish 寫進列的 source，
# bot 只拿它和這個常數比、不印快照裡的字
SOURCE_CBOE_FALLBACK = "CBOE（備援）"
TEXT_PROVISIONAL = "（暫定）"
TEXT_PROVISIONAL_CLOSE = "收盤是 CBOE 的暫定值，Nasdaq 正式資料到了會更新"
# 2026-10-24 第 9 輪批 1・A2 軌（RSI、落後／休市標示、0050 日期）：句子由 bot 組，這裡只有固定句（不寫年份，日期用 {md}）
TEXT_RSI_NOTE = "RSI14：0–100，70 以上偏熱、30 以下偏冷（描述狀態）"  # ETF、0050 細節卡有顯示 RSI 時的註腳
TEXT_CLOSE_PENDING = "{md} 收盤資料尚未取得"  # 副標題：預期的最新場次已經收完、來源還沒給（{md}＝那一天，例 10/23）
# 休市標示寫快照產生日的月／日（{md}），不寫「今天」：卡片留在聊天紀錄裡隔天再看，「今天」就變成錯的（2026-10-28 批 1 審查 F2-3）
TEXT_TW_CLOSED = "{md} 休市"  # 副標題：預期交易日被日曆覆寫成休市，而且覆寫已確認
TEXT_TW_CLOSED_TENTATIVE = "{md} 休市（暫定）"  # 副標題：覆寫是管線推定的（颱風…），還沒確認
TEXT_TW_ETF_LAG = "0050／成分資料到 {md}（除權息資料待更新）"  # 除權息資料（TWT49U）還沒涵蓋到最新場次：0050 與成分只能顯示更早一天
TEXT_ACCUMULATING = "資料累積中（{n}/5）"
TEXT_CALENDAR_PENDING = "日曆待更新"
TEXT_NOT_UPDATED = "未更新"
TEXT_MEMBER_LIST_DATE = "成分清單 {md}"
# 川普壓力指數（TACO 指數；2026-10-24 Andrew 選用 MacroMargin 的公開頁，取代「MM 川普壓力指數：抓不到（要登入）」——MM 的頁面
# 對程式回 403＋Cloudflare 驗證，不繞過）。卡上只有這些固定句＋程式算的數字與日期；來源網站的任何文字都不存、不印。
TEXT_TACO_NAME = "川普壓力指數"
TEXT_TACO_NAME_SHORT = "川普壓力"
TEXT_TACO_RANK = "任期內第 {p} 百分位"
TEXT_TACO_RANK_SHORT = "第 {p} 百分位"
TEXT_TACO_STATUS_HIGH = "高壓"
TEXT_TACO_CHANGE = "20 個交易日變化：{chg}"
TEXT_TACO_POSITION = "位階：{since} 起 {n} 個交易日中第 {p} 百分位（最高 {hi}，{hi_md}；最低 {lo}，{lo_md}）"  # n 是交易日筆數，不是日曆天（2026-10-28 批 1 審查 F2-2）
TEXT_TACO_DATA_DAY = "資料日 {md}"  # 細節卡頁尾：川普壓力指數這一筆是哪一天的（來源常比其他列晚一個場次；批 1 審查 F2-1）
# 說明句寫出主詞（重點摘要裡它和第 6 列中間最多隔 5 行）、連百分位一起說（最容易被讀錯的是「第 80 百分位＝80% 會退讓」）——
# 第 2 輪複審 seat4 R2-N2。宏觀 5/5 用一般註記的上限（NOTE_MAX 60），這句 29 字
TEXT_TACO_MEANING = "川普壓力指數越高代表壓力越大；數值和百分位都不是退讓的機率"
TEXT_TACO_SOURCE = "來源：MacroMargin（ocmacro.com），不是財經M平方的 MM 指數"
TEXT_TACO_COMPOSITION = "組成：淨支持率 20%、10 年美債 25%、MOVE 5%、標普500 22.5%、VIX 5%、CPI 即時預測 22.5%"
TEXT_TACO_METHOD_CHANGED = "方法版本已更新，組成請看來源網站"
# 來源 index.status.key 的值 → 卡上的詞：只收保存的真實回應裡看得到的鍵（2026-10-22 是 "high"）。頁面圖例另有低壓／中壓兩個區間，
# 但資料裡沒有它們的鍵與門檻，不猜；不認得的鍵（含 None）不寫狀態詞。
TACO_STATUS_WORDS: dict[str, str] = {"high": TEXT_TACO_STATUS_HIGH}
TEXT_US_NO_DIVIDEND = "美股收盤已調整分割、配息不還原"
TEXT_TW_ADJUSTED = "台股用還原除權息收盤"
TEXT_VOL_SOURCE_DIFF = "來源不同，暫不判斷"
TEXT_OI_DATE = "未平倉推定前一交易日結算（來源未標日期）"
TEXT_CHAIN_RANGE = "收到的有效合約範圍"
TEXT_WALL_RANGE = "買權牆看現價到 +15%、賣權牆看 −15% 到現價"
TEXT_WALL_DEF = "牆＝未平倉合約最集中的履約價"
# 牆距的基準（2026-10-24 第 9 輪批 1 B 軌，審查 r9 seat1 I1；Andrew 對 Q2 照預設＝收盤價）：publish 在「同一份期權鏈自己的
# close 可用」時把距離對 close 算（dist_basis＝close、ref_close＝那個 close），否則退回現價（spot）；牆的位置與 GEX 一律用現價。
# 卡上印的價格（「收盤 X」或「最新價 X」）與下面的註記都由 dist_basis 查表，不照快照裡的字組句。
DIST_CLOSE, DIST_SPOT = "close", "spot"
DIST_BASES = (DIST_CLOSE, DIST_SPOT)
TEXT_WALL_DIST_CLOSE = "牆距以收盤價計"
TEXT_WALL_DIST_SPOT = "牆距以最新價計"
WALL_DIST_TEXT = {DIST_CLOSE: TEXT_WALL_DIST_CLOSE, DIST_SPOT: TEXT_WALL_DIST_SPOT}
# 幾檔的基準不同時，兩張卡的寫法不同（審查 r9 B1 的複審：摘要卡的牆那幾列沒有印價格，不能叫人去看「各檔標示的價格」）：
# 期權細節卡每檔都在標題列印了「收盤 X」或「最新價 X」→ MIXED 指向它們；摘要卡直接點名各檔（NAMED，pairs＝「QQQ 以收盤價、SOXX 以最新價」）
TEXT_WALL_DIST_MIXED = "牆距以各檔標示的收盤價或最新價計"
TEXT_WALL_DIST_NAMED = "牆距：{pairs}計"
WALL_DIST_WORD = {DIST_CLOSE: "收盤價", DIST_SPOT: "最新價"}
TEXT_MAX_PAIN = "最大痛點＝讓選擇權買方整體虧損最多的結算價（不是目標價）"
TEXT_RATE_ASSUMED = "假設利率 4%"
TEXT_GEX_NEGATIVE = "假設部位下，現價附近 gamma 為負（漲跌較容易被放大的組合）"
TEXT_GEX_POSITIVE = "假設部位下，現價附近 gamma 為正（漲跌較容易被緩和的組合）"
TEXT_GEX_NEAR_ZERO = "假設部位下，現價附近 gamma 接近 0、不確定"
TEXT_GEX_NO_CROSSING = "±15% 內沒有變號"
TEXT_GEX_CROSSINGS = "變號點（價格由低往高）"
TEXT_COVERAGE_LOWER = "（下限）"
TEXT_SEASON_NOTE = "歷史統計，不是今年的預測；S&P 的統計不能直接套用到 SOXX"
TEXT_SEASON_SP_BASIS = "月均價口徑"
TEXT_SEASON_TW_BASIS = "月底收盤口徑"
# 2026-10-24 第 9 輪 A14：季節性的半導體兩段（SOXX 自身在前、SOX 長歷史在後）。句子不寫年份：樣本起迄從 market_seasonality.json
# 帶出來（{range}）、月份是 {month}；SOXX 的指數 2021 年換過，註記用「中途換過」不寫年。只寫「上漲比例」，不寫「勝率」。
TEXT_SEASON_PRICE_BASIS = "月底收盤、價格口徑"
TEXT_SEASON_SOXX_HEAD = "半導體 ETF（SOXX）{month} 月，" + TEXT_SEASON_PRICE_BASIS
TEXT_SEASON_SOXX_Q4 = "半導體 ETF（SOXX）第四季（12 月底比 9 月底）"
TEXT_SEASON_SOXX_MIDTERM = "期中選舉年的 {month} 月（SOXX）"
TEXT_SEASON_SOXX_MIDTERM_Q4 = "期中選舉年的第四季（SOXX）"
TEXT_SEASON_SOXX_FEW_MIDTERM = "SOXX 的期中選舉年樣本太少，不列"
TEXT_SEASON_SOXX_SAMPLE = "SOXX 樣本 {range}"
TEXT_SEASON_SOXX_DATA = "資料：Nasdaq 收盤價，已調整分割"
TEXT_SEASON_SOX_HEAD = "費城半導體（SOX）{month} 月，" + TEXT_SEASON_PRICE_BASIS
TEXT_SEASON_SOX_Q4 = "費城半導體（SOX）第四季（12 月底比 9 月底）"
TEXT_SEASON_SOX_MIDTERM = "期中選舉年的 {month} 月（SOX）"
TEXT_SEASON_SOX_MIDTERM_Q4 = "期中選舉年的第四季（SOX）"
TEXT_SEASON_SOX_SAMPLE = "SOX 樣本 {range}"
TEXT_SEASON_SOX_DATA = "資料：Nasdaq 費城半導體指數日線收盤"
TEXT_SEASON_SEMI_NOTE = "SOXX 追蹤的指數中途換過（原追蹤 SOX，後改追蹤 NYSE 半導體指數），兩者每月漲跌略有差異"
TEXT_SEASON_BASIS_NOTE = "價格口徑是依資料推定，來源沒有明說是否含配息"
TEXT_ELECTION_NOTE = "兩平台結算規則不同，不是民調、不是保證"
TEXT_POLL_NONE = "民調：未提供"
TEXT_KALSHI_TIME = "成交時間未知"
TEXT_TRUMP_NOTE = "第三方存檔，未經核實是否已發生、不代表政策已執行"
TEXT_TRUMP_NONE = "近 48 小時沒有符合主題的貼文"
TEXT_AT_LEAST = "（至少）"
TEXT_13F_LAG = "13F 季末後 45 天才申報完"
TEXT_13F_PERIOD = "彙總期間來源未標；前十大持有人申報期間以 {md} 為主（混合）"
TEXT_13F_PERIOD_SINGLE = "彙總期間來源未標；前十大持有人申報期間 {md}"
TEXT_13F_SUMMARY = "前十大成分股中，{more} 檔的加碼機構多於減碼機構（{missing} 檔資料暫缺）"
TEXT_CFTC_NOTE = "CFTC 是週資料、槓桿基金不是「外資」"
TEXT_CFTC_NO_SEMI = "CFTC 沒有半導體期貨，期貨部位只看標普與那斯達克"  # 2026-10-24：SOXX 沒有對應的期貨部位，卡上明說
TEXT_PE_NONPOSITIVE = "近四季淨利合計 ≤0，不計本益比"
TEXT_PE_MISSING = "近四季資料不齊，不計本益比"
TEXT_PE_NO_CAP = "市值暫缺，不計本益比"
TEXT_PE_STALE = "資料過期，不計本益比"
TEXT_SPY_WEIGHT = "依 S&P 500 權重（SPY 每日持股 {md}）"
TEXT_NO_US_SYMBOL = "另有 {n} 檔無美國代號（合計 {pct}%）"
# ETF 細節卡的財報行（2026-10-24 第 9 輪 A16）：這檔自己前十大的財報日（美東日期）；只有 estimated 為真才標預估；日期缺的（來源常常
# 是空的，也可能本來就不在 45 天內）不點名，只用 OTHERS 這一句；一檔都沒列出時用 NONE（不寫「其他」）
TEXT_EARNINGS_HEAD = "前十大 45 天內財報（美東日期）"  # 只看這檔 ETF 自己的前十大（批 1 審查 F2-15）
TEXT_EARNINGS_ESTIMATED = "預估"
TEXT_EARNINGS_OTHERS = "其他成分股的日期暫缺或不在 45 天內"
TEXT_EARNINGS_NONE = "成分股的日期暫缺或不在 45 天內"

# 程式自己句子的禁用字（§6）；免責句是精確例外；13F 的「加碼／減碼」描述機構，允許。
FORBIDDEN_WORDS = ("建議", "進場", "出場", "停損", "抄底", "逢低", "布局", "可以買", "該賣", "該買", "買點", "賣點")

# 量能的白話（一句話、卡片列都用這一份；VOL_LABELS 是資料值，這裡是顯示的字）
VOL_PHRASE = {
    VOL_LABEL_STRONG_SHRINK: "量明顯偏少",
    VOL_LABEL_SHRINK: "量偏少",
    VOL_LABEL_NORMAL: "量正常",
    VOL_LABEL_EXPAND: "量偏多",
    VOL_LABEL_SURGE: "爆量",
}


# ── 共用錯誤 ────────────────────────────────────────────────────────────────
# vendor（2026-10-23 審查 r7b F2）：Nasdaq 回 data: null 而且 status.rCode 不是 200＝來源自己報錯，和「沒有資料」分開記
PARSE_ERROR_KINDS = ("format", "missing_field", "empty", "too_few", "date", "conflict", "vendor")


class ParseError(ValueError):
    """sources 的解析錯誤；`kind` 是列舉值（outcome 只記 kind，detail 只放程式自己的字，不放來源內容）。"""

    def __init__(self, kind: str, detail: str = "") -> None:
        if kind not in PARSE_ERROR_KINDS:
            raise ValueError(f"unknown ParseError kind: {kind!r}")
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}" if detail else kind)

    def __reduce__(self):  # 讓子程序間傳遞時 kind 不會遺失
        return (type(self), (self.kind, self.detail))


# ── 小工具 ──────────────────────────────────────────────────────────────────
def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _pos(value: Any) -> bool:
    return _is_num(value) and value > 0


def _nonneg_int(value: Any) -> int | None:
    """OI 這類「≥0 的整數」：int，或剛好是整數的 float（CBOE 的 88.0）；bool、負數、小數 → None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and math.isfinite(value) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """dataclass／namedtuple 用屬性、dict 用鍵（sources 的型別在這裡只認欄位名）。"""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _plain_date(value: Any) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError("expected a datetime.date (not datetime)")
    return value


def _epoch(value: Any) -> float:
    """aware datetime 或 epoch 秒 → epoch 秒；naive datetime 不收（時區不明）。"""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime")
        return value.timestamp()
    if _is_num(value):
        return float(value)
    raise TypeError("expected an aware datetime or epoch seconds")


def _midnight_tpe(day: date) -> float:
    return datetime.combine(day, time(0, 0), tzinfo=TPE).timestamp()


def _month_end(day: date) -> date:
    return date(day.year, day.month, _calendar.monthrange(day.year, day.month)[1])


def fmt_md(day: date) -> str:
    """卡上的日期寫法「10/16」（固定句不寫年份，避開 validator 的年份規則）。"""
    return f"{day.month}/{day.day}"


# ── 外部字串清洗（§19.1） ─────────────────────────────────────────────────────
_STRIP_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
# 換行、tab、U+2028／2029 換成空白再刪，免得前後兩個字黏在一起
_SPACE_LIKE = frozenset("\t\n\r\x0b\x0c\x1c\x1d\x1e\x1f\x85\u2028\u2029")
_TAG_RE = re.compile(r"</?[A-Za-z!][^<>]*>")
# 「xxx://」不加前綴限制：「營收678http://192.168.0.1/」的 h 前面是數字，原本被 lookbehind 擋掉沒抓到
# （2026-10-23 審查 r8 gp2 N3）；lookbehind 只留給 www. 與「網域.tld/」兩種
_URL_RE = re.compile(
    r"(?i)(?:[a-z][a-z0-9+.\-]*://|(?<![a-z0-9])(?:www\.|[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.[a-z]{2,}/))"
)
# 沒有斜線的裸網域（「AMAZON.COM INC」「請上 example.com」）刻意不擋：持股名稱要留著；Flex 的 text 與
# card_pages（html.escape、不加連結）都不會把它變成可點的連結（2026-10-18 核對）。
_EMAIL_RE = re.compile(r"(?i)[a-z0-9._%+\-]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.[a-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)09\d{2}[-\s.]?\d{3}[-\s.]?\d{3}(?!\d)")
_FULLWIDTH_SPACE = "　"


def _drop_bad_chars(text: str) -> str:
    out: list[str] = []
    for ch in text:
        if ch in _SPACE_LIKE:
            out.append(" ")
        elif unicodedata.category(ch) not in _STRIP_CATEGORIES:
            out.append(ch)
    return "".join(out)


def clean_external(text: Any, max_len: int) -> str | None:
    """外部字串（新聞標題、川普摘錄、持股名稱、預測市場標題）入庫前與組卡前都用這個；None＝整則丟掉。

    順序照 §19.1：NFKC → 刪 Cc／Cf／Cs／Co／Cn／Zl／Zp → 去 HTML → 含網址／email／09 手機號整則丟掉
    → 「@」「＠」換全形空白 → 截長度。email 要在換掉「@」之前查才查得到，所以檢查放在換字之前（結果相同）；
    HTML 實體還原後可能又冒出控制字元（例 &#x202E;），所以還原後再洗一次。
    """
    if isinstance(max_len, bool) or not isinstance(max_len, int) or max_len <= 0:
        raise ValueError("max_len must be a positive int")
    if not isinstance(text, str):
        return None
    # 先截長（2026-10-18 安全審查 I1）：網址／email 的正規式最壞是 O(n²)，一個幾十萬字的欄位就能讓 collect 卡到被 kill；
    # 輸出只取前 max_len 字，往後看 8 倍已足夠判斷（NFKC 可能變長，所以之後再截一次）
    window = max(8 * max_len, 512)
    cleaned = _drop_bad_chars(unicodedata.normalize("NFKC", text[:window]))[:window]
    cleaned = html.unescape(_TAG_RE.sub(" ", cleaned))
    cleaned = _drop_bad_chars(unicodedata.normalize("NFKC", cleaned))[:window]
    # 編碼過的標籤（&lt;b&gt;、&#xff1c;b&#xff1e;）還原、NFKC 之後才變成 <b>：再去一次（2026-10-18）
    cleaned = _TAG_RE.sub(" ", cleaned)
    cleaned = re.sub(r" {2,}", " ", cleaned).strip()
    if not cleaned:
        return None
    if _blocked(cleaned):
        return None
    cleaned = cleaned.replace("@", _FULLWIDTH_SPACE).replace("＠", _FULLWIDTH_SPACE).strip()
    if len(cleaned) > max_len:
        cleaned = _truncate(cleaned, max_len)
        # 截完再驗一次：切口可能剛好湊出「www.」（「台股 www 上漲…」→「台股 www...」），下一次清洗才丟就不冪等
        # （2026-10-23 審查 r7a N5）
        if _blocked(cleaned):
            return None
    return cleaned.strip() or None


def _blocked(text: str) -> bool:
    """含網址／email／09 手機號：整則丟掉。便宜的預檢：網址一定有「/」或「www.」，email 一定有「@」
    （NFKC 後全形＠已是 @）。"""
    has_url = ("/" in text or "www." in text.lower()) and _URL_RE.search(text)
    return bool(has_url or ("@" in text and _EMAIL_RE.search(text)) or _PHONE_RE.search(text))


_ELLIPSIS = "..."  # ASCII：「…」經 NFKC 會變成「...」，入庫、publish、卡片各洗一次長度會再被切
_TRAILING_PUNCT = " ,.;:!?、，。；：！？-–—|｜"


def _run_char(ch: str) -> bool:
    """數字、英文、小數點、千分位、%：切在這種字的中間會把「1144點」切成「11」、把名字切一半。

    非 ASCII 的數字（阿拉伯-印度數字等）也算：手機號規則的 \\d 認得它們，退格不認得的話會切出像手機號的一段
    （2026-10-23 審查 r8 gp2 N2）。"""
    return (ch.isascii() and (ch.isalnum() or ch in ".,%")) or ch.isdecimal()


# 一個數字（正負號、千分位、小數、%）：截斷時整個留或整個丟，切一半就變成另一個數字（2026-10-23 審查 r7 #1、#2）。
# 千分位一定是逗號＋3 位數：「1,234,567」是一個數字，「2330,2317」是兩個
_NUMBER_RE = re.compile(r"[+\-−]?(?:\d{1,3}(?:,\d{3})+(?!\d)|\d+)(?:\.\d+)?%?")


def number_safe_cut(text: str, cut: int) -> int:
    """切點落在某個數字中間就退到那個數字前面；沒切到數字就原樣回傳。"""
    for match in _NUMBER_RE.finditer(text):
        if match.start() >= cut:
            break
        if cut < match.end():
            return match.start()
    return cut


def _truncate(text: str, max_len: int) -> str:
    """截到 max_len 以內並補「...」（2026-10-18 審查 r6c C1：硬切會把數字切成另一個數字、沒有標記）。

    切點落在數字／英文的中間就往回退到那一串的前面。整段都是英數、退光了的話，英文可以從中間切，數字不行
    （2026-10-23 審查 r7 #1：原本退光就硬切，「2891」變「28」、長串數字切出像手機號碼的一段，再洗一次就整則不見）。
    最後的切點一律再過 number_safe_cut：數字連前面的正負號整個留或整個丟，不留懸空的「+」，「AA+」「Disney+」
    的加號照留（r8 審查）。切完只剩標點、或只剩一個切不開的數字 → 回空字串（整則丟掉）；max_len ≤ 3 也不切數字。"""
    if max_len <= len(_ELLIPSIS):
        return text[: number_safe_cut(text, max_len)]
    keep = max_len - len(_ELLIPSIS)
    cut = keep
    if _run_char(text[keep]) and _run_char(text[keep - 1]):
        while cut and _run_char(text[cut - 1]):
            cut -= 1
    cut = number_safe_cut(text, cut or keep)
    head = text[:cut].rstrip(_TRAILING_PUNCT)
    return head + _ELLIPSIS if head else ""


# ── 交易日曆 ────────────────────────────────────────────────────────────────
# NYSE 官方表：https://www.nyse.com/markets/hours-calendars（2026-10-17 台北時間抄寫；
# 原文另存在開發紀錄）。
NYSE_HOLIDAYS = frozenset(
    {
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
        date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
        date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31),
        date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25), date(2027, 12, 24),
    }
)
NYSE_EARLY_CLOSES = frozenset({date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)})
NYSE_TABLE_FROM, NYSE_TABLE_TO = date(2026, 1, 1), date(2027, 12, 31)
_MAX_GAP_DAYS = 15  # 連假最長也不會超過；找不到就當日曆未知


@dataclass(frozen=True)
class TradingCalendar:
    """一個市場的預期交易日。表內：平日且不在休市表；表之前：用實際有成交的日期（observed）；其他＝未知。"""

    market: str  # "us" / "tw"
    tz: ZoneInfo
    close: time
    holidays: frozenset[date]
    table_from: date
    table_to: date
    early_closes: frozenset[date] = frozenset()
    early_close: time | None = None
    extra_sessions: frozenset[date] = frozenset()  # 週末照常開市（表內）
    observed: frozenset[date] = frozenset()  # 表開始之前，實際有成交的日期（SPY／FinMind TAIEX）
    closed_overrides: frozenset[date] = frozenset()  # 臨時休市（有獨立證據才寫入，§18.3）

    @cached_property
    def _observed_range(self) -> tuple[date, date] | None:
        return (min(self.observed), max(self.observed)) if self.observed else None

    def is_session(self, day: date) -> bool | None:
        """True／False；表外又沒有觀察資料 → None（未知，不猜）。"""
        if day in self.closed_overrides:
            return False
        if self.table_from <= day <= self.table_to:
            if day in self.extra_sessions:
                return True
            return day.weekday() < 5 and day not in self.holidays
        span = self._observed_range
        if day < self.table_from and span is not None and span[0] <= day <= span[1]:
            return day in self.observed
        return None

    def _step(self, day: date, direction: int) -> date | None:
        for offset in range(1, _MAX_GAP_DAYS + 1):
            candidate = day + timedelta(days=direction * offset)
            state = self.is_session(candidate)
            if state is None:
                return None
            if state:
                return candidate
        return None

    def next_session(self, day: date) -> date | None:
        return self._step(day, 1)

    def prev_session(self, day: date) -> date | None:
        return self._step(day, -1)

    def add_sessions(self, day: date, count: int) -> date | None:
        """往後（count>0）或往前（count<0）數第 |count| 個交易日；碰到未知 → None。"""
        current: date | None = day
        for _ in range(abs(count)):
            current = self._step(current, 1 if count > 0 else -1)
            if current is None:
                return None
        return current

    def sessions_ending(self, day: date, count: int) -> list[date]:
        """≤ day 的最近 count 個交易日（由舊到新）；碰到未知就停，所以可能不足 count 個。"""
        state = self.is_session(day)
        if state is None:
            return []
        out: list[date] = []
        current: date | None = day if state else self.prev_session(day)
        while current is not None and len(out) < count:
            out.append(current)
            current = self.prev_session(current)
        out.reverse()
        return out

    def sessions_between(self, start: date, end: date) -> list[date] | None:
        """[start, end] 內的交易日；中間有未知 → None。"""
        out: list[date] = []
        day = start
        while day <= end:
            state = self.is_session(day)
            if state is None:
                return None
            if state:
                out.append(day)
            day += timedelta(days=1)
        return out

    def close_at(self, day: date) -> datetime:
        local = self.early_close if (day in self.early_closes and self.early_close) else self.close
        return datetime.combine(day, local, tzinfo=self.tz)

    def last_complete_session(self, now: datetime | float, delay: timedelta) -> date | None:
        """最近一個「當地已過收盤＋delay」的預期交易日（美股 D、台股 T）；日曆未知 → None。"""
        local_now = datetime.fromtimestamp(_epoch(now), tz=self.tz)
        today = local_now.date()
        state = self.is_session(today)
        if state is None:
            return None
        if state and local_now >= self.close_at(today) + delay:
            return today
        return self.prev_session(today)


def nyse_calendar(
    *,
    holidays: Collection[date] = NYSE_HOLIDAYS,
    early_closes: Collection[date] = NYSE_EARLY_CLOSES,
    table_from: date = NYSE_TABLE_FROM,
    table_to: date = NYSE_TABLE_TO,
    extra_sessions: Iterable[date] = (),
    observed: Iterable[date] = (),
    closed_overrides: Iterable[date] = (),
) -> TradingCalendar:
    return TradingCalendar(
        market="us",
        tz=ET,
        close=time(16, 0),
        early_close=time(13, 0),
        holidays=frozenset(holidays),
        early_closes=frozenset(early_closes),
        table_from=table_from,
        table_to=table_to,
        extra_sessions=frozenset(extra_sessions),
        observed=frozenset(observed),
        closed_overrides=frozenset(closed_overrides),
    )


def twse_calendar(
    *,
    holidays: Collection[date],
    table_from: date,
    table_to: date,
    extra_sessions: Iterable[date] = (),
    observed: Iterable[date] = (),
    closed_overrides: Iterable[date] = (),
) -> TradingCalendar:
    """holidays＝休市表裡「休市」與「市場無交易，僅辦理結算交割」的平日（§18.4 的分類由 sources 做）。"""
    return TradingCalendar(
        market="tw",
        tz=TPE,
        close=time(13, 30),
        holidays=frozenset(holidays),
        table_from=table_from,
        table_to=table_to,
        extra_sessions=frozenset(extra_sessions),
        observed=frozenset(observed),
        closed_overrides=frozenset(closed_overrides),
    )


@dataclass(frozen=True)
class Calendars:
    nyse: TradingCalendar
    twse: TradingCalendar


def _calendar_of(calendars: Any, name: str) -> TradingCalendar:
    cal = calendars.get(name) if isinstance(calendars, Mapping) else getattr(calendars, name, None)
    if not isinstance(cal, TradingCalendar):
        raise ValueError(f"calendar {name!r} missing")
    return cal


def _holiday_set(holidays: Any) -> frozenset[date]:
    if isinstance(holidays, TradingCalendar):
        return holidays.holidays | holidays.closed_overrides
    return frozenset(holidays or ())


def _third_weekday(year: int, month: int, weekday: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 14)


def us_monthly_expiry_day(year: int, month: int, holidays: Any) -> date:
    """美股月選到期日：第三個週五；那天休市就提前到週四。"""
    friday = _third_weekday(year, month, 4)
    return friday - timedelta(days=1) if friday in _holiday_set(holidays) else friday


def tw_settlement_day(year: int, month: int, calendar: TradingCalendar | None = None) -> date:
    """台指月結算日：第三個週三；那天休市就順延到下一個交易日（期交所契約規格；日曆未知就用原日）。"""
    wednesday = _third_weekday(year, month, 2)
    if calendar is None or calendar.is_session(wednesday) is not False:
        return wednesday
    return calendar.next_session(wednesday) or wednesday


def is_monthly_expiry(day: date, holidays: Any) -> bool:
    """§4：週五且 15–21 日，或該週五休市時的週四 14–20 日。"""
    if day.weekday() == 4 and 15 <= day.day <= 21:
        return True
    return day.weekday() == 3 and 14 <= day.day <= 20 and (day + timedelta(days=1)) in _holiday_set(holidays)


def monthly_expiry(expiries: Iterable[date], holidays: Any, after: date | None = None) -> date | None:
    """從收到的到期日挑 `after`（資料日 D）之後第一個月選；找不到 → None（卡上標未知）。"""
    for expiry in sorted(set(expiries)):
        if after is not None and expiry <= after:
            continue
        if is_monthly_expiry(expiry, holidays):
            return expiry
    return None


def volume_day_flag(day: date, calendar: TradingCalendar) -> str | None:
    """量能的「結算日／早收日」（§18.15：美股月選結算的第三個週五、台指第三個週三、NYSE 早收）。"""
    if calendar.market == "us":
        if day == us_monthly_expiry_day(day.year, day.month, calendar):
            return FLAG_SETTLEMENT
        if day in calendar.early_closes:
            return FLAG_EARLY_CLOSE
        return None
    if day == tw_settlement_day(day.year, day.month, calendar):
        return FLAG_SETTLEMENT
    return None


def txo_front_month(data_date: date, calendar: TradingCalendar | None = None) -> str:
    """台指最近月選：結算日 > 資料日的第一個月份（結算日當天期交所仍列當月，要跳過，§18.13）。"""
    year, month = data_date.year, data_date.month
    if tw_settlement_day(year, month, calendar) <= data_date:
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return f"{year:04d}{month:02d}"


# ── 新鮮度（§5 類型表＋§18.1、§18.9） ─────────────────────────────────────────
KIND_US_DAILY = "us_daily"  # 美股日線、ETF、VIX、美債、宏觀（Yahoo／代理，每個代號一筆）
KIND_OPTIONS = "options"
KIND_TW_DAILY = "tw_daily"  # ALLBUT0999、BFI82U、融資、期交所、本益比
KIND_BTC = "btc"
KIND_PREDICTION = "prediction"
KIND_NEWS = "news"
KIND_TRUMP = "trump"
KIND_EVENT = "event"
KIND_CFTC = "cftc"
KIND_FINRA = "finra"
KIND_TW_MONTHLY = "tw_monthly"  # 月營收（表上沒有，比照 FINRA 以月底為錨）
KIND_US_CPI = "us_cpi"  # 美國 CPI／核心 CPI（BLS 月資料，2026-10-24）：同樣以月底為錨＋62 天
KIND_13F = "13f"
KIND_US_FUND = "us_fund"
KIND_HOLDINGS_SPY = "holdings_spy"
KIND_HOLDINGS_NDX = "holdings_ndx"
KIND_HOLDINGS_MONEYDJ = "holdings_moneydj"
KIND_SEASON = "season"

_SESSION_KINDS = {
    KIND_US_DAILY: "nyse",
    KIND_HOLDINGS_SPY: "nyse",
    KIND_HOLDINGS_NDX: "nyse",
    KIND_TW_DAILY: "twse",
}
_INSTANT_KINDS = {
    KIND_BTC: BTC_TTL,
    KIND_PREDICTION: PREDICTION_TTL,
    KIND_NEWS: NEWS_TTL,
    KIND_TRUMP: TRUMP_TTL,
    KIND_EVENT: EVENT_GRACE,
}
_DAY_KINDS = {KIND_CFTC: CFTC_DAYS, KIND_13F: F13_DAYS, KIND_US_FUND: FUND_DAYS, KIND_HOLDINGS_MONEYDJ: MONEYDJ_DAYS}
_MONTH_END_KINDS = {KIND_FINRA: MONTH_END_DAYS, KIND_TW_MONTHLY: MONTH_END_DAYS, KIND_US_CPI: MONTH_END_DAYS}
STALE_KINDS = tuple(_SESSION_KINDS) + (KIND_OPTIONS,) + tuple(_INSTANT_KINDS) + tuple(_DAY_KINDS) + tuple(
    _MONTH_END_KINDS
) + (KIND_SEASON,)


@dataclass(frozen=True)
class Staleness:
    at: float  # epoch 秒；bot 只比 now > at
    calendar_pending: bool = False  # 表外，用了 +6 個日曆日的退路（卡上標「日曆待更新」）
    not_updated: bool = False  # 美股基本面：已經過了「最新應有季度」的申報期限（卡上標「未更新」）


def stale_after_info(kind: str, data_date: Any, calendars: Any, now: datetime | float) -> Staleness:
    """依資料類型算過期時間點。

    - 交易日類（美股日線／ETF／VIX／美債／宏觀、持股清單 SPY／Nasdaq-100、台股日資料）：資料日之後第 4 個
      預期交易日的收盤時刻。
    - 期權：資料日（`last_trade_time` 的美東日期）之後下一個預期 NYSE 交易日收盤＋3 小時（§18.1）。
    - 日曆表外：資料日＋6 個日曆日的收盤時刻（期權再＋3 小時）並標 calendar_pending（§18.3）。
    - 時刻類（BTC、預測市場＝抓取時間；新聞／川普＝pubDate；事件＝UTC 時刻）：data_date 傳 aware datetime。
    - 日期類：CFTC 報告日＋16 天、13F 申報期間眾數＋155 天、美股基本面季度結束＋160 天、MoneyDJ 清單日＋62 天、
      FINRA／月營收以月底為錨＋62 天；都取台北時間該日 0 點。
    - 季節性：靜態表（年度維護），樣本截止年＋2 年的 4/1 才算過期（data_date 傳截止年或該年任一天）。
    """
    if kind in _SESSION_KINDS or kind == KIND_OPTIONS:
        cal = _calendar_of(calendars, _SESSION_KINDS.get(kind, "nyse"))
        day = _plain_date(data_date)
        target = cal.add_sessions(day, STALE_SESSIONS) if kind in _SESSION_KINDS else cal.next_session(day)
        grace = OPTIONS_GRACE if kind == KIND_OPTIONS else timedelta(0)
        if target is None:
            fallback = datetime.combine(day + timedelta(days=CALENDAR_FALLBACK_DAYS), cal.close, tzinfo=cal.tz)
            return Staleness((fallback + grace).timestamp(), calendar_pending=True)
        return Staleness((cal.close_at(target) + grace).timestamp())
    if kind in _INSTANT_KINDS:
        return Staleness(_epoch(data_date) + _INSTANT_KINDS[kind].total_seconds())
    if kind in _DAY_KINDS:
        day = _plain_date(data_date)
        not_updated = False
        if kind == KIND_US_FUND:
            not_updated = _epoch(now) > _midnight_tpe(day + timedelta(days=FUND_NOT_UPDATED_DAYS))
        return Staleness(_midnight_tpe(day + timedelta(days=_DAY_KINDS[kind])), not_updated=not_updated)
    if kind in _MONTH_END_KINDS:
        day = _month_end(_plain_date(data_date))
        return Staleness(_midnight_tpe(day + timedelta(days=_MONTH_END_KINDS[kind])))
    if kind == KIND_SEASON:
        year = data_date if isinstance(data_date, int) and not isinstance(data_date, bool) else None
        if year is None:
            year = _plain_date(data_date).year
        return Staleness(_midnight_tpe(date(year + 2, 4, 1)))
    raise ValueError(f"unknown stale kind: {kind!r}")


def stale_after(kind: str, data_date: Any, calendars: Any, now: datetime | float) -> float:
    """INTERFACES 的介面：只回 epoch 秒（旗標要用 `stale_after_info`）。"""
    return stale_after_info(kind, data_date, calendars, now).at


def stale_min(values: Iterable[float | None]) -> float | None:
    """衍生列（寬度、量能、牆、GEX、一句話、矛盾…）：所有輸入的最小值；有一個輸入沒有 → None。"""
    out: float | None = None
    for value in values:
        if value is None:
            return None
        out = value if out is None else min(out, value)
    return out


def common_date(dates: Iterable[date | None]) -> date | None:
    """衍生列的資料日＝輸入的共同日期；不一致或有缺 → None。"""
    unique = set(dates)
    if len(unique) != 1:
        return None
    (only,) = unique
    return only


def cboe_snapshot_ok(fetched_at: datetime | float, cboe_timestamp: datetime | float) -> bool:
    """§18.1 拒收：抓取時間 − CBOE `timestamp` > 12 小時就不收，保留上一份。"""
    return _epoch(fetched_at) - _epoch(cboe_timestamp) <= CBOE_MAX_AGE.total_seconds()


def et_date(moment: datetime) -> date:
    """CBOE `last_trade_time` 等美東時間的日期（期權、VIX 的資料日，§18.1）。"""
    if moment.tzinfo is None:
        return moment.date()  # sources 已註明是美東當地時間
    return moment.astimezone(ET).date()


def fomc_decision_at(second_day: date) -> datetime:
    """FOMC 的事件時刻＝第二天 14:00 美東（§18.9）。"""
    return datetime.combine(second_day, time(14, 0), tzinfo=ET)


# ── 均線、RSI、回撤 ───────────────────────────────────────────────────────────
def sma(values: Sequence[float], n: int) -> float | None:
    if n <= 0 or len(values) < n:
        return None
    window = values[-n:]
    if not all(_is_num(v) for v in window):
        return None
    return math.fsum(window) / n


def ma_distance_pct(close: float | None, ma: float | None) -> float | None:
    if not _pos(close) or not _pos(ma):
        return None
    return (close / ma - 1.0) * 100.0


def rsi_wilder(closes: Sequence[float], period: int = RSI_PERIOD) -> float | None:
    """Wilder RSI：前 period 個變化取簡單平均，之後 (前值×(period−1)＋當期)/period。"""
    if period <= 0 or len(closes) < period + 1 or not all(_is_num(c) for c in closes):
        return None
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gains = [max(c, 0.0) for c in changes]
    losses = [max(-c, 0.0) for c in changes]
    avg_gain = math.fsum(gains[:period]) / period
    avg_loss = math.fsum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return None if avg_gain == 0 else 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def drawdown_pct(closes: Sequence[float], window: int = DRAWDOWN_WINDOW) -> float | None:
    """收盤相對近 window 根最高收盤的回撤（%，≤0）。"""
    if len(closes) < window or not all(_pos(c) for c in closes[-window:]):
        return None
    tail = closes[-window:]
    return (tail[-1] / max(tail) - 1.0) * 100.0


def _sessions_upto(calendar: Any, day: date, count: int) -> list[date]:
    """呼叫端可以給 TradingCalendar，或已排好的預期交易日清單。"""
    if isinstance(calendar, TradingCalendar):
        return calendar.sessions_ending(day, count)
    days = sorted({d for d in calendar if d <= day})
    return days[-count:]


@dataclass(frozen=True)
class Technicals:
    close: float | None
    prev_close: float | None
    chg: float | None
    chg_pct: float | None
    ma: dict[int, float | None]
    ma_dist: dict[int, float | None]
    above: dict[int, bool | None]
    dd240_pct: float | None
    rsi14: float | None


def technicals(
    closes: Mapping[date, float],
    calendar: Any,
    day: date,
    quarantine: Sequence[tuple[date, date | None]] | None = None,
) -> Technicals:
    """單一 ETF／指數在 D 的技術面；均線要窗口內每個預期交易日都有收盤，日變化要前一筆＝prev_session(D)。

    quarantine＝這檔的隔離區間（分割沒對上、跳空）：碰到的窗口（日變化、均線、240 日回檔、RSI 往回算的那段）
    一律不算，和成分寬度同一個規則（2026-10-23 審查 r7 #5：0050 被隔離了，技術面照樣發布 -50%）。"""
    sessions = _sessions_upto(calendar, day, DRAWDOWN_WINDOW + 20)
    empty = {n: None for n in MA_WINDOWS}
    if not sessions or sessions[-1] != day or not _pos(closes.get(day)):
        return Technicals(None, None, None, None, dict(empty), dict(empty), dict(empty), None, None)
    series = [closes.get(s) for s in sessions]
    last = len(series) - 1

    def usable(span: int) -> bool:  # 以 D 結尾的窗口：夠長、每天有收盤、沒碰到隔離區間（和 breadth 同一個判斷）
        return len(series) >= span and _window_valid(series, sessions, quarantine, last, span)

    close = float(series[-1])
    prev = series[-2] if usable(2) else None
    chg = close - prev if prev is not None else None
    chg_pct = (close / prev - 1.0) * 100.0 if prev is not None else None
    ma: dict[int, float | None] = {}
    for n in MA_WINDOWS:
        ma[n] = sma(series[-n:], n) if usable(n) else None
    ma_dist = {n: ma_distance_pct(close, ma[n]) for n in MA_WINDOWS}
    above = {n: (None if ma[n] is None else _position(close, ma[n]) > 0) for n in MA_WINDOWS}
    tail: list[float] = []
    for session, value in zip(reversed(sessions), reversed(series)):
        if not _pos(value) or _touches_quarantine(quarantine, session, session):
            break
        tail.append(float(value))
    tail.reverse()
    return Technicals(
        close=close,
        prev_close=prev,
        chg=chg,
        chg_pct=chg_pct,
        ma=ma,
        ma_dist=ma_dist,
        above=above,
        dd240_pct=drawdown_pct(series, DRAWDOWN_WINDOW) if usable(DRAWDOWN_WINDOW) else None,
        rsi14=rsi_wilder(tail, RSI_PERIOD) if len(tail) >= RSI_MIN_CLOSES else None,
    )


# ── 寬度、漲跌家數、新高新低 ──────────────────────────────────────────────────
QuarantineMap = Mapping[str, Sequence[tuple[date, date | None]]]


def _position(close: float, ma: float) -> int:
    if math.isclose(close, ma, rel_tol=1e-9, abs_tol=1e-12):
        return 0
    return 1 if close > ma else -1


def _touches_quarantine(intervals: Sequence[tuple[date, date | None]] | None, start: date, end: date) -> bool:
    """隔離區間 (from, to)；to 為 None＝還沒解除。窗口 [start, end] 碰到任何一段就無效。"""
    for interval in intervals or ():
        begin, finish = interval[0], interval[1]
        if begin <= end and (finish is None or finish >= start):
            return True
    return False


def _window_valid(
    series: Sequence[float | None],
    sessions: Sequence[date],
    intervals: Sequence[tuple[date, date | None]] | None,
    end: int,
    span: int,
) -> bool:
    """以 end 結尾、span 個交易日的窗口：每天都有收盤，而且沒碰到隔離區間。"""
    start = end - span + 1
    return all(_pos(v) for v in series[start : end + 1]) and not _touches_quarantine(
        intervals, sessions[start], sessions[end]
    )


def _universe(closes_by_symbol: Mapping[str, Mapping[date, float]], members: Iterable[str] | None) -> list[str]:
    source = closes_by_symbol.keys() if members is None else members
    return list(dict.fromkeys(source))


@dataclass(frozen=True)
class Breadth:
    n: int
    above: int
    below: int
    equal: int
    new_below: int
    new_above: int
    valid: int
    total: int
    prev_pct: float | None  # 5 個交易日前（兩天都有效的同一批）
    prev_valid: int  # 那一批的家數
    same_set_pct: float | None  # 同一批在 D 的比例（和 prev_pct 對照）
    status: str  # "ok"（有效 ≥80%）／"low_coverage"

    @property
    def pct(self) -> float | None:
        return 100.0 * self.above / self.valid if self.valid else None

    @property
    def coverage(self) -> float:
        return self.valid / self.total if self.total else 0.0


def breadth(
    closes_by_symbol: Mapping[str, Mapping[date, float]],
    calendar: Any,
    D: date,
    n: int,
    *,
    members: Iterable[str] | None = None,
    quarantine: QuarantineMap | None = None,
) -> Breadth:
    """成分股站上／跌破 N 日線的家數（§4、§18.2）。

    有效＝D 往前 N+5 個預期交易日每天都有收盤、且窗口沒碰到該檔的隔離區間（只影響碰到的那條均線）。
    今天新跌破／新站上用 D−1 的均線比；5 個交易日前用「D 與 D−5 都有效」的同一批，交集 <80% 不比。
    """
    universe = _universe(closes_by_symbol, members)
    total = len(universe)
    span = n + BREADTH_EXTRA
    sessions = _sessions_upto(calendar, D, span + BREADTH_EXTRA)
    if n <= 0 or len(sessions) < span or sessions[-1] != D:
        return Breadth(n, 0, 0, 0, 0, 0, 0, total, None, 0, None, STATUS_LOW_COVERAGE)
    last = len(sessions) - 1
    has_prev_window = len(sessions) >= span + BREADTH_EXTRA
    counts = Counter()
    same_now = same_prev = same_total = 0
    for symbol in universe:
        closes = closes_by_symbol.get(symbol) or {}
        series = [closes.get(s) for s in sessions]
        intervals = (quarantine or {}).get(symbol)
        if not _window_valid(series, sessions, intervals, last, span):
            continue
        counts["valid"] += 1
        pos_now = _position(series[last], math.fsum(series[last - n + 1 : last + 1]) / n)
        pos_prev_day = _position(series[last - 1], math.fsum(series[last - n : last]) / n)
        counts[{1: "above", 0: "equal", -1: "below"}[pos_now]] += 1
        if pos_now < 0 <= pos_prev_day:
            counts["new_below"] += 1
        if pos_now > 0 >= pos_prev_day:
            counts["new_above"] += 1
        back = last - BREADTH_EXTRA
        if has_prev_window and _window_valid(series, sessions, intervals, back, span):
            same_total += 1
            same_now += pos_now > 0
            same_prev += _position(series[back], math.fsum(series[back - n + 1 : back + 1]) / n) > 0
    comparable = same_total > 0 and same_total >= COVERAGE_OK * total
    valid = counts["valid"]
    return Breadth(
        n=n,
        above=counts["above"],
        below=counts["below"],
        equal=counts["equal"],
        new_below=counts["new_below"],
        new_above=counts["new_above"],
        valid=valid,
        total=total,
        prev_pct=100.0 * same_prev / same_total if comparable else None,
        prev_valid=same_total,
        same_set_pct=100.0 * same_now / same_total if comparable else None,
        status=STATUS_OK if total and valid >= COVERAGE_OK * total else STATUS_LOW_COVERAGE,
    )


@dataclass(frozen=True)
class AdCounts:
    adv: int
    dec: int
    flat: int
    valid: int
    total: int


def ad_counts(
    closes_by_symbol: Mapping[str, Mapping[date, float]],
    calendar: Any,
    D: date,
    *,
    members: Iterable[str] | None = None,
    quarantine: QuarantineMap | None = None,
) -> AdCounts:
    """美股漲跌家數：D 對 prev_session(D)，兩天都有收盤、沒碰隔離區間才算。"""
    universe = _universe(closes_by_symbol, members)
    sessions = _sessions_upto(calendar, D, 2)
    if len(sessions) < 2 or sessions[-1] != D:
        return AdCounts(0, 0, 0, 0, len(universe))
    prev_day = sessions[0]
    counts = Counter()
    for symbol in universe:
        closes = closes_by_symbol.get(symbol) or {}
        now_close, prev_close = closes.get(D), closes.get(prev_day)
        if not (_pos(now_close) and _pos(prev_close)):
            continue
        if _touches_quarantine((quarantine or {}).get(symbol), prev_day, D):
            continue
        counts["valid"] += 1
        counts[{1: "adv", 0: "flat", -1: "dec"}[_position(now_close, prev_close)]] += 1
    return AdCounts(counts["adv"], counts["dec"], counts["flat"], counts["valid"], len(universe))


@dataclass(frozen=True)
class HighsLows:
    new_high: int
    new_low: int
    valid: int
    total: int


def highs_lows_240(
    closes_by_symbol: Mapping[str, Mapping[date, float]],
    calendar: Any,
    D: date,
    *,
    members: Iterable[str] | None = None,
    quarantine: QuarantineMap | None = None,
) -> HighsLows:
    """240 日新高／新低：245 根都有效者，D 收盤嚴格高於（低於）前 239 根。"""
    universe = _universe(closes_by_symbol, members)
    span = HIGHLOW_WINDOW + BREADTH_EXTRA
    sessions = _sessions_upto(calendar, D, span)
    if len(sessions) < span or sessions[-1] != D:
        return HighsLows(0, 0, 0, len(universe))
    counts = Counter()
    for symbol in universe:
        closes = closes_by_symbol.get(symbol) or {}
        series = [closes.get(s) for s in sessions]
        if not all(_pos(v) for v in series):
            continue
        if _touches_quarantine((quarantine or {}).get(symbol), sessions[0], sessions[-1]):
            continue
        counts["valid"] += 1
        before = series[-HIGHLOW_WINDOW:-1]
        counts["new_high"] += series[-1] > max(before)
        counts["new_low"] += series[-1] < min(before)
    return HighsLows(counts["new_high"], counts["new_low"], counts["valid"], len(universe))


def day_coverage(
    closes_by_symbol: Mapping[str, Mapping[date, float]], members: Iterable[str] | None, day: date
) -> tuple[int, int]:
    universe = _universe(closes_by_symbol, members)
    have = sum(1 for symbol in universe if _pos((closes_by_symbol.get(symbol) or {}).get(day)))
    return have, len(universe)


def latest_complete_day(
    closes_by_symbol: Mapping[str, Mapping[date, float]],
    members: Iterable[str] | None,
    days: Iterable[date],
    threshold: float = COVERAGE_OK,
) -> date | None:
    """只發布覆蓋率達 80% 的最新那一天（當天還在抓時就是前一個完整交易日）。"""
    member_list = list(members) if members is not None else None
    for day in sorted(set(days), reverse=True):
        have, total = day_coverage(closes_by_symbol, member_list, day)
        if total and have >= threshold * total:
            return day
    return None


# ── 量能 ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class VolumeState:
    ratio: float | None
    pct250: float | None
    label: str | None
    pv_label: str | None
    day_flag: str | None = None  # 結算日／早收日：照樣顯示，但不觸發量能類矛盾
    note: str | None = None  # 「來源不同，暫不判斷」


def volume_label(ratio: float | None) -> str | None:
    if ratio is None:
        return None
    if ratio <= VOL_STRONG_SHRINK:
        return VOL_LABEL_STRONG_SHRINK
    if ratio <= VOL_SHRINK:
        return VOL_LABEL_SHRINK
    if ratio < VOL_EXPAND:
        return VOL_LABEL_NORMAL
    if ratio < VOL_SURGE:
        return VOL_LABEL_EXPAND
    return VOL_LABEL_SURGE


def _volume_ok(value: Any) -> bool:
    return _is_num(value) and value >= 0


def volume_state(
    volumes: Mapping[date, float],
    D: date,
    *,
    calendar: Any = None,
    closes: Mapping[date, float] | None = None,
    day_flag: str | None = None,
    excluded: Collection[date] = (),
    quarantine: Sequence[tuple[date, date | None]] | None = None,
) -> VolumeState:
    """倍數＝D 量／前 20 個交易日平均（不含 D）；百分位＝近 250 個交易日（含 D）量 ≤ 當天的比例。

    calendar 沒給就用 volumes 的日期當交易日；excluded＝來源不同的日子（CBOE 備援量，§18.7）：
    D 本身是 → 不判斷；窗口裡有 → 當缺值。價量組合需要 closes（D 與前一個交易日都要有）。
    quarantine＝這檔的隔離區間：分割前後的量不同口徑，碰到的窗口不算倍數／百分位（2026-10-23 審查 r7 #5）。
    """
    sessions = _sessions_upto(calendar if calendar is not None else volumes.keys(), D, VOL_PCT_DAYS)
    if not sessions or sessions[-1] != D:
        return VolumeState(None, None, None, None, day_flag)
    # 備援的日子先判：VOO 的 CBOE 報價沒有量，不能變成「暫缺」、和其他三檔的「暫不判斷」兩種寫法（審查 r9 Codex 席）
    if D in excluded:
        return VolumeState(None, None, None, None, day_flag, TEXT_VOL_SOURCE_DIFF)
    if not _volume_ok(volumes.get(D)):
        return VolumeState(None, None, None, None, day_flag)

    def usable(day: date) -> bool:
        return day not in excluded and _volume_ok(volumes.get(day))

    today = float(volumes[D])
    prior = sessions[-(VOL_AVG_DAYS + 1) : -1]
    ratio = None
    if (
        len(prior) == VOL_AVG_DAYS
        and all(usable(day) for day in prior)
        and not _touches_quarantine(quarantine, prior[0], D)
    ):
        average = math.fsum(float(volumes[day]) for day in prior) / VOL_AVG_DAYS
        ratio = today / average if average > 0 else None
    pct250 = None
    if (
        len(sessions) == VOL_PCT_DAYS
        and all(usable(day) for day in sessions)
        and not _touches_quarantine(quarantine, sessions[0], D)
    ):
        pct250 = 100.0 * sum(1 for day in sessions if float(volumes[day]) <= today) / VOL_PCT_DAYS
    pv_label = None
    if closes is not None and ratio is not None and len(sessions) >= 2:
        now_close, prev_close = closes.get(D), closes.get(sessions[-2])
        if _pos(now_close) and _pos(prev_close) and _position(now_close, prev_close) != 0:
            up = now_close > prev_close
            more = ratio >= PV_SPLIT
            pv_label = ("價漲" if up else "價跌") + ("量增" if more else "量縮")
    return VolumeState(ratio, pct250, volume_label(ratio), pv_label, day_flag)


# ── 期權（CBOE） ────────────────────────────────────────────────────────────
_KIND_ALIASES = {"C": "C", "CALL": "C", "買權": "C", "P": "P", "PUT": "P", "賣權": "P"}


def _norm_kind(value: Any) -> str | None:
    return _KIND_ALIASES.get(value.strip().upper()) if isinstance(value, str) else None


def parse_option_symbol(symbol: Any) -> tuple[str, date, str, float] | None:
    """「QQQ261016C00688000」→ ("QQQ", 2026-10-16, "C", 688.0)；不是標準 root 的（調整後合約）→ None。"""
    if not isinstance(symbol, str):
        return None
    match = OPTION_SYMBOL_RE.fullmatch(symbol)
    if match is None:
        return None
    root, ymd, kind, strike = match.groups()
    try:
        expiry = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:]))
    except ValueError:
        return None
    return root, expiry, kind, int(strike) / 1000.0


@dataclass(frozen=True)
class OptionLeg:
    symbol: str
    root: str
    expiry: date
    kind: str  # "C" / "P"
    strike: float
    oi: int
    days: int  # 到期日 − D（日曆天）
    volume: float | None
    iv: float | None  # 已換成價外 IV 的話是換過的值
    gamma: float | None  # CBOE 自帶的 gamma（只拿來核對）
    iv_replaced: bool = False


@dataclass(frozen=True)
class ConflictLeg:
    """被排除的衝突合約：oi 已知（各筆一致）或未知（取各筆最大值放進分母，覆蓋率因此是下限）。"""

    expiry: date | None
    strike: float | None
    days: int | None  # 到期日 − D
    oi: int | None
    oi_max: int | None


@dataclass(frozen=True)
class OptionStats:
    raw: int
    bad: int
    conflict: int
    oi_valid: int
    gamma_valid: int
    iv_replaced: int
    expired: int
    conflict_legs: tuple[ConflictLeg, ...] = ()
    unattributed_conflicts: int = 0  # 來源只給筆數、沒給是哪幾筆
    unattributed_oi: int = 0  # 來源給了那些筆數的 OI 合計時，放進分母


def _contract_fields(contract: Any) -> tuple:
    """比對同代號的重複列用；NaN 彼此不相等，先換成 None，免得完全相同的兩列被當成衝突。"""
    values = (_get(contract, name) for name in ("oi", "volume", "iv", "gamma", "delta", "strike", "expiry", "kind"))
    return tuple(None if isinstance(v, float) and not math.isfinite(v) else v for v in values)


def option_sets(
    chain: Any, D: date, *, spot: float | None = None, root: str | None = None
) -> tuple[list[OptionLeg], list[OptionLeg], OptionStats]:
    """§4 的兩個集合。

    OI 有效：代號符合標準 root、OI ≥0 整數、履約價 >0、剩餘期限 >0（到期日 > D）；同代號值衝突 → 都丟。
    gamma 有效：OI 有效＋CBOE gamma 有限＋IV 有限且 >0；IV=0 的價內合約改用同到期、同履約價另一邊（價外）
    的 IV（記數），找不到就排除。chain 可以是 sources 的 OptionChain（contracts、raw_count、bad_count、
    conflict_count、spot…）或直接一串合約。
    """
    contracts = list(_get(chain, "contracts", chain) if not isinstance(chain, (list, tuple)) else chain)
    spot = spot if spot is not None else _get(chain, "spot")
    if not _pos(spot):
        raise ValueError("spot must be positive")
    raw = _get(chain, "raw_count") if not isinstance(chain, (list, tuple)) else None
    raw = raw if isinstance(raw, int) and not isinstance(raw, bool) else len(contracts)
    bad = _nonneg_int(_get(chain, "bad_count", 0) if not isinstance(chain, (list, tuple)) else 0) or 0
    upstream_conflicts = (
        _nonneg_int(_get(chain, "conflict_count", 0) if not isinstance(chain, (list, tuple)) else 0) or 0
    )
    if root is None and not isinstance(chain, (list, tuple)) and _get(chain, "symbol") in OPTION_ROOTS:
        root = _get(chain, "symbol")
    if root is None:
        roots = Counter(parsed[0] for c in contracts if (parsed := parse_option_symbol(_get(c, "symbol"))))
        root = roots.most_common(1)[0][0] if roots else None

    by_symbol: dict[str, list[Any]] = {}
    for contract in contracts:
        parsed = parse_option_symbol(_get(contract, "symbol"))
        if parsed is None or parsed[0] != root:
            bad += 1
            continue
        by_symbol.setdefault(_get(contract, "symbol"), []).append(contract)

    conflict_legs: list[ConflictLeg] = []
    conflict_rows = 0
    unique: list[tuple[tuple[str, date, str, float], Any]] = []
    for symbol, rows in by_symbol.items():
        parsed = parse_option_symbol(symbol)
        if len({_contract_fields(r) for r in rows}) > 1:
            conflict_rows += len(rows)
            ois = [_nonneg_int(_get(r, "oi")) for r in rows]
            known = ois[0] if ois[0] is not None and all(o == ois[0] for o in ois) else None
            candidates = [o for o in ois if o is not None]
            conflict_legs.append(
                ConflictLeg(parsed[1], parsed[3], (parsed[1] - D).days, known, max(candidates) if candidates else None)
            )
            continue
        unique.append((parsed, rows[0]))

    chain_conflicts = _get(chain, "conflicts") if not isinstance(chain, (list, tuple)) else None
    unattributed = 0
    unattributed_oi = 0
    if chain_conflicts:
        for item in chain_conflicts:
            parsed = parse_option_symbol(_get(item, "symbol"))
            oi = _nonneg_int(_get(item, "oi"))
            if parsed is None:
                conflict_legs.append(ConflictLeg(None, None, None, oi, oi))
            else:
                conflict_legs.append(ConflictLeg(parsed[1], parsed[3], (parsed[1] - D).days, oi, oi))
    elif upstream_conflicts:
        # 來源只給筆數：不知道落在哪裡，覆蓋率只能標下限；若給的是那些列的 OI 合計（數字）就整筆放進分母
        unattributed = upstream_conflicts
        known_total = _get(chain, "conflict_oi_known")
        if not isinstance(known_total, bool):
            unattributed_oi = _nonneg_int(known_total) or 0

    oi_valid: list[OptionLeg] = []
    expired = 0
    parsed_by_key: dict[tuple[date, str, float], Any] = {}
    for parsed, contract in unique:
        _root, expiry, kind, strike = parsed
        given_kind = _get(contract, "kind")
        given_strike = _get(contract, "strike")
        given_expiry = _get(contract, "expiry")
        if (
            (given_kind is not None and _norm_kind(given_kind) != kind)
            or (given_strike is not None and not (_is_num(given_strike) and math.isclose(given_strike, strike)))
            or (given_expiry is not None and given_expiry != expiry)
        ):
            bad += 1
            continue
        parsed_by_key[(expiry, kind, strike)] = contract
        oi = _nonneg_int(_get(contract, "oi"))
        if oi is None or strike <= 0:
            bad += 1
            continue
        days = (expiry - D).days
        if days <= 0:
            expired += 1
            continue
        volume = _get(contract, "volume")
        iv = _get(contract, "iv")
        gamma = _get(contract, "gamma")
        oi_valid.append(
            OptionLeg(
                symbol=_get(contract, "symbol"),
                root=root,
                expiry=expiry,
                kind=kind,
                strike=strike,
                oi=oi,
                days=days,
                volume=float(volume) if _volume_ok(volume) else None,
                iv=float(iv) if _is_num(iv) else None,
                gamma=float(gamma) if _is_num(gamma) else None,
            )
        )

    gamma_valid: list[OptionLeg] = []
    replaced = 0
    for leg in oi_valid:
        if leg.gamma is None or leg.iv is None or leg.iv < 0:
            continue
        if leg.iv > 0:
            gamma_valid.append(leg)
            continue
        in_the_money = leg.strike < spot if leg.kind == "C" else leg.strike > spot
        if not in_the_money:
            continue
        other = parsed_by_key.get((leg.expiry, "P" if leg.kind == "C" else "C", leg.strike))
        other_iv = _get(other, "iv") if other is not None else None
        if _pos(other_iv):
            gamma_valid.append(replace(leg, iv=float(other_iv), iv_replaced=True))
            replaced += 1

    stats = OptionStats(
        raw=raw,
        bad=bad,
        conflict=upstream_conflicts + conflict_rows,
        oi_valid=len(oi_valid),
        gamma_valid=len(gamma_valid),
        iv_replaced=replaced,
        expired=expired,
        conflict_legs=tuple(conflict_legs),
        unattributed_conflicts=unattributed,
        unattributed_oi=unattributed_oi,
    )
    return oi_valid, gamma_valid, stats


def _in_window(leg: OptionLeg, window_days: int = OPTION_WINDOW_DAYS) -> bool:
    return 0 < leg.days <= window_days


def _both_sides(strikes: Iterable[float], spot: float, band: float = NEAR_BAND) -> bool:
    values = list(strikes)
    below = any(spot * (1 - band) <= k < spot for k in values)
    above = any(spot < k <= spot * (1 + band) for k in values)
    return below and above


def _max_oi_strike(oi_by_strike: Mapping[float, int], low: float, high: float, spot: float) -> float | None:
    """[low, high] 內 OI 最大的履約價；並列取離現價近的；全是 0 → None。"""
    best: tuple[int, float, float] | None = None
    for strike, oi in oi_by_strike.items():
        if not (low <= strike <= high) or oi <= 0:
            continue
        key = (-oi, abs(strike - spot), strike)
        if best is None or key < best:
            best = key
    return None if best is None else best[2]


def _aggregate(legs: Iterable[Any]) -> tuple[dict[float, int], dict[float, int]]:
    calls: dict[float, int] = {}
    puts: dict[float, int] = {}
    for leg in legs:
        target = calls if leg.kind == "C" else puts
        target[leg.strike] = target.get(leg.strike, 0) + leg.oi
    return calls, puts


def _dist_pct(strike: float | None, spot: float) -> float | None:
    return None if strike is None else (strike / spot - 1.0) * 100.0


@dataclass(frozen=True)
class Walls:
    call_wall: float | None
    put_wall: float | None
    call_wall_dist_pct: float | None
    put_wall_dist_pct: float | None
    status: str
    n_contracts: int


def _walls_from(legs: Sequence[Any], spot: float, min_contracts: int) -> Walls:
    if len(legs) < min_contracts or not _both_sides((leg.strike for leg in legs), spot):
        return Walls(None, None, None, None, STATUS_INSUFFICIENT, len(legs))
    calls, puts = _aggregate(legs)
    call_wall = _max_oi_strike(calls, spot, spot * (1 + WALL_BAND), spot)
    put_wall = _max_oi_strike(puts, spot * (1 - WALL_BAND), spot, spot)
    return Walls(call_wall, put_wall, _dist_pct(call_wall, spot), _dist_pct(put_wall, spot), STATUS_OK, len(legs))


def walls(
    oi_valid: Sequence[OptionLeg],
    spot: float,
    *,
    window_days: int = OPTION_WINDOW_DAYS,
    min_contracts: int = WALL_MIN_CONTRACTS,
) -> Walls:
    """D 之後 45 天內到期、OI 有效 ≥100 筆且現價 ±10% 兩邊都有才算；按履約價加總 OI。"""
    return _walls_from([leg for leg in oi_valid if _in_window(leg, window_days)], spot, min_contracts)


def max_pain(legs: Sequence[Any], spot: float) -> float | None:
    """到期時買賣方總內含價值最小的履約價（候選＝這些合約的履約價）；並列取離現價近的。"""
    if not legs or sum(leg.oi for leg in legs) <= 0:
        return None
    calls, puts = _aggregate(legs)
    best: tuple[float, float, float] | None = None
    for price in sorted(set(calls) | set(puts)):
        value = math.fsum(oi * (price - k) for k, oi in calls.items() if price > k) + math.fsum(
            oi * (k - price) for k, oi in puts.items() if k > price
        )
        key = (value, abs(price - spot), price)
        if best is None or key < best:
            best = key
    return best[2]


def _complete_month(legs: Sequence[Any], spot: float) -> bool:
    call_strikes = {leg.strike for leg in legs if leg.kind == "C"}
    put_strikes = {leg.strike for leg in legs if leg.kind == "P"}
    return (
        len(legs) >= MONTHLY_MIN_CONTRACTS
        and len(call_strikes) >= MONTHLY_MIN_STRIKES
        and len(put_strikes) >= MONTHLY_MIN_STRIKES
        and _both_sides(call_strikes | put_strikes, spot)
    )


@dataclass(frozen=True)
class MonthlyMetrics:
    expiry: date
    call_wall: float | None
    put_wall: float | None
    max_pain: float | None
    status: str
    n_contracts: int


def monthly_metrics(oi_valid: Sequence[OptionLeg], spot: float, expiry: date) -> MonthlyMetrics:
    """月選自己的 OI 有效 ≥40、買權與賣權各 ≥15 個履約價、現價 ±10% 兩邊都有，才算它的牆與最大痛點。"""
    legs = [leg for leg in oi_valid if leg.expiry == expiry]
    if not _complete_month(legs, spot):
        return MonthlyMetrics(expiry, None, None, None, STATUS_INSUFFICIENT, len(legs))
    calls, puts = _aggregate(legs)
    return MonthlyMetrics(
        expiry=expiry,
        call_wall=_max_oi_strike(calls, spot, spot * (1 + WALL_BAND), spot),
        put_wall=_max_oi_strike(puts, spot * (1 - WALL_BAND), spot, spot),
        max_pain=max_pain(legs, spot),
        status=STATUS_OK,
        n_contracts=len(legs),
    )


@dataclass(frozen=True)
class Pcr:
    oi: float | None
    volume: float | None


def pcr(oi_valid: Sequence[OptionLeg], *, window_days: int = OPTION_WINDOW_DAYS) -> Pcr:
    """賣權／買權（OI、量各一）；45 天視窗內 OI 有效的合約；分母 0 → 缺值。"""
    legs = [leg for leg in oi_valid if _in_window(leg, window_days)]
    call_oi = sum(leg.oi for leg in legs if leg.kind == "C")
    put_oi = sum(leg.oi for leg in legs if leg.kind == "P")
    call_vol = math.fsum(leg.volume for leg in legs if leg.kind == "C" and leg.volume is not None)
    put_vol = math.fsum(leg.volume for leg in legs if leg.kind == "P" and leg.volume is not None)
    return Pcr(put_oi / call_oi if call_oi > 0 else None, put_vol / call_vol if call_vol > 0 else None)


class GexCrossing(NamedTuple):
    price: float
    direction: str  # "up"＝價格往上穿過時由負轉正；"down"＝由正轉負


# 變號方向的中文說明：價格由低往高穿過該點時（圖的標籤與卡片的「變號點」都用這一份）
GEX_DIRECTION_TEXT = {"up": "由負轉正", "down": "由正轉負"}


@dataclass(frozen=True)
class GexResult:
    status: str  # "ok" / "insufficient"（±10% 覆蓋率 <80% 或沒有 gamma 有效合約）
    at_spot: float | None
    sign: int | None  # GEX(S_now) 的實際正負號
    crossings: tuple[GexCrossing, ...]  # 離現價最近的兩個變號點（依價格排序）
    near_zero: bool | None  # |GEX(S_now)| < ±15% 網格上 max|GEX| 的 2%
    coverage: float | None
    coverage_is_lower_bound: bool
    grid: tuple[float, ...] = ()
    values: tuple[float, ...] = ()
    max_abs: float | None = None
    check_at_spot: float | None = None  # 用 CBOE 自帶 gamma 算的現價 GEX（核對重算用）


def bs_gamma(spot: Any, strike: Any, years: Any, sigma: Any, rate: float) -> Any:
    """Black-Scholes gamma（無股息）；參數可以是 numpy 陣列，依廣播規則向量化。"""
    import numpy as np  # 只有算 GEX 才載入（bot 端不能載 numpy）

    spot = np.asarray(spot, dtype=float)
    strike = np.asarray(strike, dtype=float)
    years = np.asarray(years, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    root_t = np.sqrt(years)
    d1 = (np.log(spot / strike) + (rate + 0.5 * sigma**2) * years) / (sigma * root_t)
    return np.exp(-0.5 * d1**2) / (math.sqrt(2.0 * math.pi) * spot * sigma * root_t)


def _gex_values(prices: Any, legs: Sequence[OptionLeg], rate: float) -> Any:
    import numpy as np

    prices = np.asarray(prices, dtype=float)[:, None]
    strikes = np.array([leg.strike for leg in legs], dtype=float)[None, :]
    years = np.array([leg.days / 365.0 for leg in legs], dtype=float)[None, :]
    sigma = np.array([leg.iv for leg in legs], dtype=float)[None, :]
    signed_oi = np.array([leg.oi if leg.kind == "C" else -leg.oi for leg in legs], dtype=float)[None, :]
    gamma = bs_gamma(prices, strikes, years, sigma, rate)
    return (gamma * signed_oi).sum(axis=1) * CONTRACT_MULTIPLIER * prices[:, 0] ** 2 * 0.01


def _crossings(grid: Sequence[float], values: Sequence[float], tolerance: float) -> list[GexCrossing]:
    out: list[GexCrossing] = []
    last_index: int | None = None
    last_value = 0.0
    for index, value in enumerate(values):
        if abs(value) <= tolerance:
            continue
        if last_index is not None and (value > 0) != (last_value > 0):
            if index == last_index + 1:
                price = grid[last_index] + (grid[index] - grid[last_index]) * last_value / (last_value - value)
            else:  # 中間有一段剛好是 0，取那段的中點
                price = (grid[last_index + 1] + grid[index - 1]) / 2.0
            out.append(GexCrossing(float(price), "up" if value > 0 else "down"))
        last_index, last_value = index, value
    return out


def gex_from_sets(
    oi_valid: Sequence[OptionLeg], gamma_valid: Sequence[OptionLeg], stats: OptionStats, spot: float, rate: float
) -> GexResult:
    """GEX(S)＝Σ（買權＋、賣權−）gamma(S)×OI×100×S²×1%（假設造市商持有買權、賣出賣權）。

    計算用 45 天視窗內的 gamma 有效合約；覆蓋率＝其中履約價在現價 ±10% 的 OI ÷ 同範圍 OI 有效的 OI
    （衝突排除的：OI 已知計入分母；未知取最大候選值計入並標下限；來源只給筆數 → 標下限）。
    """
    if not _pos(spot):
        raise ValueError("spot must be positive")
    if not (_is_num(rate) and -0.05 < rate < 0.5):
        raise ValueError("rate must be a decimal (e.g. 0.0426)")
    low, high = spot * (1 - NEAR_BAND), spot * (1 + NEAR_BAND)

    def near(leg: Any) -> bool:
        return low <= leg.strike <= high

    window_oi = [leg for leg in oi_valid if _in_window(leg)]
    legs = [leg for leg in gamma_valid if _in_window(leg)]
    denominator = sum(leg.oi for leg in window_oi if near(leg))
    lower_bound = stats.unattributed_conflicts > 0
    denominator += stats.unattributed_oi
    for conflict in stats.conflict_legs:
        if conflict.days is None or conflict.strike is None:
            lower_bound = True  # 連是哪一檔都不知道，不能確定在不在範圍內
            continue
        if not (0 < conflict.days <= OPTION_WINDOW_DAYS and low <= conflict.strike <= high):
            continue
        if conflict.oi is not None:
            denominator += conflict.oi
        else:
            lower_bound = True
            denominator += conflict.oi_max or 0
    numerator = sum(leg.oi for leg in legs if near(leg))
    coverage = numerator / denominator if denominator > 0 else None
    if not legs or coverage is None or coverage < GEX_COVERAGE_OK:
        return GexResult(STATUS_INSUFFICIENT, None, None, (), None, coverage, lower_bound)

    import numpy as np

    grid = np.linspace(GEX_GRID_LOW, GEX_GRID_HIGH, GEX_GRID_POINTS) * spot
    values = _gex_values(grid, legs, rate)
    at_spot = float(_gex_values([spot], legs, rate)[0])
    max_abs = float(np.max(np.abs(values)))
    near_zero = max_abs == 0.0 or abs(at_spot) < GEX_NEAR_ZERO * max_abs
    found = _crossings(grid.tolist(), values.tolist(), 1e-9 * max_abs)
    nearest = sorted(sorted(found, key=lambda c: abs(c.price - spot))[:2], key=lambda c: c.price)
    check = math.fsum(
        (leg.gamma if leg.kind == "C" else -leg.gamma) * leg.oi * CONTRACT_MULTIPLIER * spot**2 * 0.01 for leg in legs
    )
    return GexResult(
        status=STATUS_OK,
        at_spot=at_spot,
        sign=(at_spot > 0) - (at_spot < 0),
        crossings=tuple(nearest),
        near_zero=near_zero,
        coverage=coverage,
        coverage_is_lower_bound=lower_bound,
        grid=tuple(float(x) for x in grid),
        values=tuple(float(x) for x in values),
        max_abs=max_abs,
        check_at_spot=check,
    )


def gex_curve(chain: Any, spot: float, r: float, *, D: date | None = None) -> GexResult:
    """INTERFACES 的介面：先建 OI／gamma 有效集合再算；r 是小數（3M 公債 4.26% → 0.0426）。

    D 不給就用 chain.quote_last_trade_et 的美東日期（§18.1 的資料日）。
    """
    if D is None:
        moment = _get(chain, "quote_last_trade_et")
        if not isinstance(moment, datetime):
            raise ValueError("D is required when the chain has no quote_last_trade_et")
        D = et_date(moment)
    oi_valid, gamma_valid, stats = option_sets(chain, D, spot=spot)
    return gex_from_sets(oi_valid, gamma_valid, stats, spot, r)


def options_values(
    chain: Any,
    D: date,
    *,
    spot: float,
    rate: float | None,
    holidays: Any,
    iv30: float | None = None,
    ref_close: float | None = None,
) -> dict:
    """組 `us.options.<SYM>` 的 values（欄位見 ROW_IDS）；rate=None → 用 4% 並標 rate_assumed。

    ref_close（2026-10-24 第 9 輪 I1）：publish 認定可用的「同一份期權鏈自己的收盤」。給了正數 → 牆距對它算
    （dist_basis＝close、ref_close 照填）；沒給或不是正的有限數 → 對現價算（dist_basis＝spot、ref_close＝None）。
    牆的位置（以現價為中心找 ±15%）、GEX、其餘欄位都和基準無關。"""
    oi_valid, gamma_valid, stats = option_sets(chain, D, spot=spot)
    rate_assumed = rate is None
    gex = gex_from_sets(oi_valid, gamma_valid, stats, spot, DEFAULT_RATE if rate_assumed else rate)
    wall = walls(oi_valid, spot)
    close = float(ref_close) if _is_num(ref_close) and ref_close > 0 else None
    base, basis = (close, DIST_CLOSE) if close is not None else (float(spot), DIST_SPOT)
    expiry = monthly_expiry({leg.expiry for leg in oi_valid}, holidays, after=D)
    monthly = None
    if expiry is not None:
        month = monthly_metrics(oi_valid, spot, expiry)
        monthly = {
            "expiry": expiry.isoformat(),
            "call_wall": month.call_wall,
            "put_wall": month.put_wall,
            "max_pain": month.max_pain,
            "status": month.status,
        }
    ratio = pcr(oi_valid)
    iv30 = iv30 if iv30 is not None else _get(chain, "iv30") if not isinstance(chain, (list, tuple)) else None
    return {
        "spot": float(spot),
        "call_wall": wall.call_wall,
        "put_wall": wall.put_wall,
        "call_wall_dist_pct": _dist_pct(wall.call_wall, base),
        "put_wall_dist_pct": _dist_pct(wall.put_wall, base),
        "ref_close": close,
        "dist_basis": basis,
        "wall_status": wall.status,
        "monthly": monthly,
        "pcr_oi": ratio.oi,
        "pcr_vol": ratio.volume,
        "iv30": float(iv30) if _is_num(iv30) else None,
        "gex": {
            "status": gex.status,
            "at_spot": gex.at_spot,
            "sign": gex.sign,
            "crossings": [{"price": c.price, "direction": c.direction} for c in gex.crossings],
            "near_zero": gex.near_zero,
            "coverage": gex.coverage,
            "lower_bound": gex.coverage_is_lower_bound,
            "rate_assumed": rate_assumed,
        },
        "counts": {
            "raw": stats.raw,
            "bad": stats.bad,
            "conflict": stats.conflict,
            "oi_valid": stats.oi_valid,
            "gamma_valid": stats.gamma_valid,
            "iv_replaced": stats.iv_replaced,
        },
        "oi_date_note": TEXT_OI_DATE,
    }


# ── 台指選擇權 ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _TxoLeg:
    kind: str
    strike: float
    oi: int


@dataclass(frozen=True)
class TxoWalls:
    month: str
    spot: float
    call_wall: float | None
    put_wall: float | None
    max_pain: float | None
    call_wall_dist_pct: float | None
    put_wall_dist_pct: float | None
    status: str
    n_contracts: int


def txo_walls(rows: Iterable[Any], month: str, spot: float) -> TxoWalls:
    """最近月選（ContractMonth(Week)＝YYYYMM、一般盤）的牆與最大痛點；完整性門檻同美股月選。

    spot＝期交所資料日那天的加權收盤（期交所常晚一天，§18.13）。同月同履約價同買賣權出現兩個不同 OI → 都丟。
    """
    if not _pos(spot):
        raise ValueError("spot must be positive")
    seen: dict[tuple[str, float], set[int]] = {}
    for row in rows:
        if str(_get(row, "month", "")).strip() != month:
            continue
        session = _get(row, "session")
        if session is not None and session not in ("一般", "regular"):
            continue
        kind = _norm_kind(_get(row, "kind"))
        strike = _get(row, "strike")
        oi = _nonneg_int(_get(row, "oi"))
        if kind is None or not _pos(strike) or oi is None:
            continue
        seen.setdefault((kind, float(strike)), set()).add(oi)
    legs = [_TxoLeg(kind, strike, next(iter(ois))) for (kind, strike), ois in seen.items() if len(ois) == 1]
    if not _complete_month(legs, spot):
        return TxoWalls(month, spot, None, None, None, None, None, STATUS_INSUFFICIENT, len(legs))
    calls, puts = _aggregate(legs)
    call_wall = _max_oi_strike(calls, spot, spot * (1 + WALL_BAND), spot)
    put_wall = _max_oi_strike(puts, spot * (1 - WALL_BAND), spot, spot)
    return TxoWalls(
        month=month,
        spot=spot,
        call_wall=call_wall,
        put_wall=put_wall,
        max_pain=max_pain(legs, spot),
        call_wall_dist_pct=_dist_pct(call_wall, spot),
        put_wall_dist_pct=_dist_pct(put_wall, spot),
        status=STATUS_OK,
        n_contracts=len(legs),
    )


# ── 台股還原除權息 ───────────────────────────────────────────────────────────
def _rebuild(bar: Any, close: float) -> Any:
    if is_dataclass(bar) and not isinstance(bar, type):
        return replace(bar, close=close)
    if hasattr(bar, "_replace"):
        return bar._replace(close=close)
    raise TypeError("bar must be a dataclass or namedtuple with a close field")


def adjust_tw_closes(bars: Sequence[Any], exrights: Iterable[Any], *, code: str | None = None) -> list[Any]:
    """因子＝除權息參考價／除權息前收盤，只乘在除權息日之前的收盤（不調量）；多個事件連乘。

    只用已經發生的事件（除權息日 ≤ 最後一根的日期）；同一檔同一天兩筆不同因子 → ParseError("conflict")。
    """
    if not bars:
        return []
    last_day = max(_get(bar, "date") for bar in bars)
    factors: dict[date, float] = {}
    for event in exrights:
        if code is not None and str(_get(event, "code")) != code:
            continue
        day, pre, ref = _get(event, "date"), _get(event, "pre_close"), _get(event, "ref_price")
        if not (_pos(pre) and _pos(ref)) or not isinstance(day, date) or day > last_day:
            continue
        factor = ref / pre
        if day in factors and not math.isclose(factors[day], factor, rel_tol=1e-12):
            raise ParseError("conflict", "two ex-right factors on one day")
        factors[day] = factor
    ordered = sorted(factors.items())
    out = []
    for bar in bars:
        day = _get(bar, "date")
        cumulative = 1.0
        for ex_day, factor in ordered:
            if day < ex_day:
                cumulative *= factor
        close = _get(bar, "close")
        out.append(bar if cumulative == 1.0 or not _is_num(close) else _rebuild(bar, close * cumulative))
    return out


# ── 基本面、股別合併、13F ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class EpsResult:
    pe: float | None
    ttm_net_income_kusd: float | None
    quarter_ends: tuple[date, ...]
    status: str  # ok / missing / not_consecutive / conflict / nonpositive / no_market_cap
    note: str | None


def eps_metrics(quarters: Iterable[Any], market_cap_usd: float | None = None) -> EpsResult:
    """本益比＝市值 ÷ 近四季普通股淨利合計（千美元×1000）；四季要齊、連續（相鄰 75–105 天）、不重複，合計 >0。"""
    by_end: dict[date, set[float]] = {}
    for quarter in quarters:
        if isinstance(quarter, tuple) and not hasattr(quarter, "_fields"):
            end, value = quarter[0], quarter[1]
        else:
            end, value = _get(quarter, "end_date"), _get(quarter, "net_income_common_kusd")
        if not isinstance(end, date):
            continue
        by_end.setdefault(end, set()).add(float(value) if _is_num(value) else math.nan)
    if any(len(values) > 1 for values in by_end.values()):
        return EpsResult(None, None, (), "conflict", TEXT_PE_MISSING)
    latest = sorted(by_end, reverse=True)[:4]
    values = [next(iter(by_end[end])) for end in latest]
    if len(latest) < 4 or not all(math.isfinite(v) for v in values):
        return EpsResult(None, None, tuple(latest), "missing", TEXT_PE_MISSING)
    gaps = [(a - b).days for a, b in zip(latest, latest[1:])]
    if not all(75 <= gap <= 105 for gap in gaps):
        return EpsResult(None, None, tuple(latest), "not_consecutive", TEXT_PE_MISSING)
    ttm = math.fsum(values)
    if ttm <= 0:
        return EpsResult(None, ttm, tuple(latest), "nonpositive", TEXT_PE_NONPOSITIVE)
    if not _pos(market_cap_usd):
        return EpsResult(None, ttm, tuple(latest), "no_market_cap", None)
    return EpsResult(market_cap_usd / (ttm * 1000.0), ttm, tuple(latest), STATUS_OK, None)


def median_with_count(values: Iterable[float | None]) -> tuple[float | None, int]:
    """中位數附有效家數（None 不算）。"""
    usable = [float(v) for v in values if _is_num(v)]
    return (statistics.median(usable) if usable else None), len(usable)


SHARE_CLASS_GROUPS = (("GOOGL", "GOOG"), ("FOXA", "FOX"), ("NWSA", "NWS"), ("BRK.B", "BRK.A"))
_SHARE_CLASS_KEY = {symbol: group[0] for group in SHARE_CLASS_GROUPS for symbol in group}


@dataclass(frozen=True)
class MergedHolding:
    name: str
    symbols: tuple[str, ...]
    weight: float | None
    primary: str  # 13F 數字取這個股別（市值較大：market_values 或持股列的 market_cap；都沒有就看權重）


def merge_share_classes(holdings: Iterable[Any], market_values: Mapping[str, float] | None = None) -> list[MergedHolding]:
    """同一公司的不同股別（GOOGL＋GOOG、FOXA／FOX、NWSA／NWS、BRK.B／BRK.A）合併成一家，權重相加，依權重排序。"""
    groups: dict[str, list[Any]] = {}
    order: list[str] = []
    for holding in holdings:
        symbol = _get(holding, "symbol")
        if not isinstance(symbol, str):
            continue
        key = _SHARE_CLASS_KEY.get(symbol, symbol)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(holding)
    merged: list[MergedHolding] = []
    for key in order:
        members = groups[key]
        weights = [_get(h, "weight") for h in members]
        known = [float(w) for w in weights if _is_num(w)]

        def size(holding: Any) -> float:
            symbol = _get(holding, "symbol")
            if market_values and _is_num(market_values.get(symbol)):
                return float(market_values[symbol])
            if _is_num(_get(holding, "market_cap")):  # Nasdaq-100 名單給的是市值
                return float(_get(holding, "market_cap"))
            weight = _get(holding, "weight")
            return float(weight) if _is_num(weight) else -1.0

        primary = max(members, key=size)
        merged.append(
            MergedHolding(
                name=str(_get(primary, "name") or _get(primary, "symbol")),
                symbols=tuple(_get(h, "symbol") for h in members),
                weight=math.fsum(known) if known else None,
                primary=_get(primary, "symbol"),
            )
        )
    return sorted(merged, key=lambda h: -(h.weight if h.weight is not None else -1.0))


def inst13f_summary(rows: Iterable[Any]) -> dict[str, int]:
    """「前十大成分股中，X 檔的加碼機構多於減碼機構（Y 檔資料暫缺）」的數字；沒資料不算成「沒有加碼」。"""
    more = missing = total = 0
    for row in rows:
        total += 1
        increased, decreased = _get(row, "increased"), _get(row, "decreased")
        if not (_is_num(increased) and _is_num(decreased)):
            missing += 1
        elif increased > decreased:
            more += 1
    return {"more_increase": more, "missing": missing, "total": total}


def holder_period_mode(periods: Iterable[date]) -> tuple[date | None, bool]:
    """前十大持有人申報期間的眾數（並列取較新的，免得永遠過期，§18.9）＋是否混合。"""
    counts = Counter(p for p in periods if isinstance(p, date))
    if not counts:
        return None, False
    mode = max(counts, key=lambda p: (counts[p], p))
    return mode, len(counts) > 1


def revenue_positive_share(revenues: Mapping[str, Any]) -> tuple[tuple[int, int] | None, int, int]:
    """0050 最新月營收年增 >0 的比例：只比同一月份（取最多家公布的那個月，並列取較新）。"""
    by_month: dict[tuple[int, int], list[float]] = {}
    for revenue in revenues.values():
        year, month, yoy = _get(revenue, "year"), _get(revenue, "month"), _get(revenue, "yoy")
        if isinstance(year, int) and isinstance(month, int) and _is_num(yoy):
            by_month.setdefault((year, month), []).append(float(yoy))
    if not by_month:
        return None, 0, 0
    month = max(by_month, key=lambda m: (len(by_month[m]), m))
    values = by_month[month]
    return month, sum(1 for v in values if v > 0), len(values)


# ── 矛盾與一句話 ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Fact:
    """一個面向：值、資料日、狀態正常與否（過期／暫缺 → ok=False，不參與一句話與矛盾）。"""

    value: Any
    data_date: date | None
    ok: bool = True


class Contra(NamedTuple):
    code: str
    subject: str | None


CONTRA_CODES = (
    "above_ma20_weak_breadth",
    "up_low_volume",
    "down_high_volume",
    "foreign_sell_trust_buy",
    "index_down_margin_up",
    "index_up_foreign_short_up",
    "gamma_pos_vix_inverted",
)
CONTRA_TEXT = {
    "above_ma20_weak_breadth": "{subject}在月線上，但成分股站上月線不到一半",
    "up_low_volume": "{subject}上漲但量偏少",
    "down_high_volume": "{subject}下跌但量偏多",
    "foreign_sell_trust_buy": "外資 5 日賣超、投信 5 日買超",
    "index_down_margin_up": "加權指數下跌、融資增加",
    "index_up_foreign_short_up": "加權指數上漲、外資台指期淨空單增加",
    "gamma_pos_vix_inverted": "{subject}假設部位 gamma 正、近月恐慌偏高",  # VIX／VIX3M > 1
}
# 主詞先中文（§6「先中文、括號英文」）；卡上每列 ≤24 個中文字寬，句型配上最長的主詞也不超過
SUBJECT_NAME = {"VOO": "標普500", "QQQ": "那斯達克", "SOXX": "半導體", "SPY": "標普500（SPY）", "0050": "台灣50", "TAIEX": "加權指數"}
US_SUBJECTS = ("VOO", "QQQ", "SOXX")
US_GAMMA_SUBJECTS = ("SPY", "QQQ", "SOXX")
_SHRINK_LABELS = (VOL_LABEL_SHRINK, VOL_LABEL_STRONG_SHRINK)
_EXPAND_LABELS = (VOL_LABEL_EXPAND, VOL_LABEL_SURGE)


def _same_day(state: Mapping[str, Fact], keys: Sequence[str]) -> list[Any] | None:
    """全部都在、狀態正常、資料日相同才回值；否則 None（不比）。"""
    facts = [state.get(key) for key in keys]
    if any(f is None or not f.ok or f.value is None or f.data_date is None for f in facts):
        return None
    if len({f.data_date for f in facts}) != 1:
        return None
    return [f.value for f in facts]


def _flagged(state: Mapping[str, Fact], subject: str) -> bool:
    flag = state.get(f"{subject}.vol_flag")
    return flag is not None and bool(flag.value)


def _volume_rules(state: Mapping[str, Fact], subject: str) -> list[Contra]:
    if _flagged(state, subject):
        return []
    values = _same_day(state, (f"{subject}.chg_pct", f"{subject}.vol_label"))
    if values is None or not _is_num(values[0]):
        return []
    change, label = values
    if change > 0 and label in _SHRINK_LABELS:
        return [Contra("up_low_volume", subject)]
    if change < 0 and label in _EXPAND_LABELS:
        return [Contra("down_high_volume", subject)]
    return []


def _breadth_rule(state: Mapping[str, Fact], subject: str) -> list[Contra]:
    values = _same_day(state, (f"{subject}.above_ma20", f"{subject}.breadth20_pct"))
    if values is not None and values[0] is True and _is_num(values[1]) and values[1] < 50:
        return [Contra("above_ma20_weak_breadth", subject)]
    return []


def contradictions(state: Mapping[str, Fact], market: str | None = None) -> list[Contra]:
    """7 條矛盾規則（§4），只用狀態正常且資料日相同的面向；回傳全部命中的，卡上取第一條。
    market＝"us"／"tw"；不給就兩邊都看（美股在前）。

    state 的鍵：「<代號>.chg_pct／.above_ma20／.breadth20_pct／.vol_label／.vol_flag／.gex_sign／.gex_near_zero」
    （美股 VOO、QQQ、SOXX、SPY；台股 0050 與 TAIEX），「VIX」「VIX3M」，「tw.foreign_5d」「tw.trust_5d」
    「tw.margin_chg」「tw.fut_net」「tw.fut_net_chg」（億元／口）。
    """
    if market is None:
        return contradictions(state, "us") + contradictions(state, "tw")
    found: list[Contra] = []
    if market == "us":
        for subject in US_SUBJECTS:
            found += _breadth_rule(state, subject)
        for subject in US_SUBJECTS:
            found += _volume_rules(state, subject)
        for subject in US_GAMMA_SUBJECTS:
            values = _same_day(state, (f"{subject}.gex_sign", f"{subject}.gex_near_zero", "VIX", "VIX3M"))
            if values is None:
                continue
            sign, near_zero, vix, vix3m = values
            if sign == 1 and near_zero is False and _pos(vix) and _pos(vix3m) and vix / vix3m > 1:
                found.append(Contra("gamma_pos_vix_inverted", subject))
        return found
    if market != "tw":
        raise ValueError("market must be 'us' or 'tw'")
    found += _breadth_rule(state, "0050")
    found += _volume_rules(state, "TAIEX")
    values = _same_day(state, ("tw.foreign_5d", "tw.trust_5d"))
    if values is not None and _is_num(values[0]) and _is_num(values[1]) and values[0] < 0 < values[1]:
        found.append(Contra("foreign_sell_trust_buy", None))
    values = _same_day(state, ("TAIEX.chg_pct", "tw.margin_chg"))
    if values is not None and _is_num(values[0]) and _is_num(values[1]) and values[0] < 0 < values[1]:
        found.append(Contra("index_down_margin_up", None))
    values = _same_day(state, ("TAIEX.chg_pct", "tw.fut_net", "tw.fut_net_chg"))
    if values is not None and all(_is_num(v) for v in values) and values[0] > 0 and values[1] < 0 and values[2] < 0:
        found.append(Contra("index_up_foreign_short_up", None))
    return found


def contra_sentence(contra: Contra) -> str:
    subject = SUBJECT_NAME.get(contra.subject or "", "")
    return CONTRA_TEXT[contra.code].format(subject=subject)


def contra_values(found: Sequence[Contra]) -> dict:
    """`contra.us`／`contra.tw` 的 values：全部命中的代碼＋卡上那一列（每張卡最多 1 列）。"""
    codes = [c.code if c.subject is None else f"{c.code}:{c.subject}" for c in found]
    return {"codes": codes, "text": contra_sentence(found[0]) if found else None}


def _fmt_yi(value: float) -> str:
    amount = abs(value)
    return f"{amount:,.0f}" if amount >= 100 else f"{amount:,.1f}"


def one_liner(state: Mapping[str, Fact], market: str) -> str:
    """固定句型的一句話（altText 開頭、卡上一列）；只放和主指數同一資料日、狀態正常的面向，缺主指數就回空字串。

    美股：「標普收在月線上、成分股站上月線 41%、量偏少」（VOO 與其 S&P 500 成分）；
    台股：「台股收在月線下、外資賣超 120 億、量偏多」（加權指數、三大法人、成交金額）。

    美股這句的主詞是「標普」不是「美股」（2026-10-24 第 9 輪 N2，審查 r9 s1-N2）：它只看 VOO，摘要輪播第一張的美股列
    把三檔分組（標普在月線上，那指、半導體在月線下）之後，「美股收在月線上」會和同一輪播裡的那一列互相矛盾。用「標普」而不是
    「標普500」：寬度和原本的「美股」一樣（最壞 23.5），不會讓美股卡放不下這一句。
    """
    if market == "us":
        anchor_key, prefix, extras = "VOO.above_ma20", "標普", ("VOO.breadth20_pct", "VOO.vol_label")
        flag_subject = "VOO"
    elif market == "tw":
        anchor_key, prefix, extras = "TAIEX.above_ma20", "台股", ("tw.foreign", "TAIEX.vol_label")
        flag_subject = "TAIEX"
    else:
        raise ValueError("market must be 'us' or 'tw'")
    anchor = state.get(anchor_key)
    if anchor is None or not anchor.ok or not isinstance(anchor.value, bool) or anchor.data_date is None:
        return ""
    parts = [f"{prefix}收在月線{'上' if anchor.value else '下'}"]
    for key in extras:
        values = _same_day(state, (anchor_key, key))
        if values is None:
            continue
        value = values[1]
        if key.endswith("breadth20_pct") and _is_num(value):
            parts.append(f"成分股站上月線 {round(value):.0f}%")
        elif key == "tw.foreign" and _is_num(value) and value != 0:
            parts.append(f"外資{'買超' if value > 0 else '賣超'} {_fmt_yi(value)} 億")
        elif key.endswith("vol_label") and value in VOL_PHRASE and not _flagged(state, flag_subject):
            parts.append(VOL_PHRASE[value])
    return "、".join(parts)


# ── 季節性列（讀 market_seasonality.json 的內容，不讀檔） ─────────────────────────
def is_midterm_year(year: int) -> bool:
    return year % 4 == 2


SEMI_MIN_MIDTERM_YEARS = 5  # 期中選舉年那兩段至少要有這麼多年才放上卡（SOXX 自身只有 2 年，SOX 有 8 年）
SEMI_MIN_COVERAGE = 0.90  # 有效月份占應有月份的比例：和 jobs/build_market_seasonality 的 MIN_COVERAGE 一樣
_YEAR_MONTH_RE = re.compile(r"(\d{4})-(\d{2})", re.ASCII)  # re.ASCII：全形、阿拉伯數字不收（第 2 輪複審 gp1 N7）


def _year_month(value: Any) -> tuple[int, int] | None:
    match = _YEAR_MONTH_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    return (year, month) if 1900 <= year <= 2100 and 1 <= month <= 12 else None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _semi_years(section: Any) -> int | None:
    """半導體一段（SOXX／SOX）整體合理才回樣本最多幾年，否則 None（整段當作 pending）：已建完（不是 pending_full_data）、
    樣本起迄與首末月份合理、有效月份不超過應有月份且至少 90%。market_seasonality.json 是靜態檔，這是擋「重建時
    拿到殘缺或壞掉的資料」的最後一道。"""
    if not isinstance(section, Mapping) or section.get("pending_full_data") is not False:
        return None
    sample = section.get("sample")
    if not (isinstance(sample, list) and len(sample) == 2 and all(_is_int(x) for x in sample)):
        return None
    first_year, last_year = sample
    if not 1900 <= first_year <= last_year <= 2100:
        return None
    years = last_year - first_year + 1
    expected, valid = section.get("months_expected"), section.get("months_valid")
    if not (_is_int(expected) and _is_int(valid) and 12 <= expected <= years * 12
            and SEMI_MIN_COVERAGE * expected <= valid <= expected):
        return None
    first, last = _year_month(section.get("first_month")), _year_month(section.get("last_month"))
    if first is None or last is None or first > last:
        return None
    return years


def _semi_stats(stats: Any, years: int, *, min_n: int = 1) -> dict | None:
    """一格統計（上漲比例、中位數、平均、年數）合理才放：比例 0–100、漲跌幅不低於 −100%、年數在 1 到樣本年數之間。"""
    if not isinstance(stats, Mapping):
        return None
    up, median, mean, n = (stats.get(key) for key in ("up_pct", "median", "mean", "n"))
    if not (_is_num(up) and 0 <= up <= 100 and _is_num(median) and -100 <= median <= 1000
            and _is_num(mean) and -100 <= mean <= 1000 and _is_int(n) and max(1, min_n) <= n <= years):
        return None
    return {"up_pct": up, "median": median, "mean": mean, "n": n}


def _semi_values(section: Any, midterm: bool, month: str) -> dict:
    """SOXX 或 SOX 在 `season.now` 裡的六個值（月、第四季、期中選舉年的月與第四季、pending、樣本起迄）。"""
    years = _semi_years(section)
    if years is None:
        return {"month": None, "q4": None, "midterm": None, "midterm_q4": None, "pending": True, "range": None}
    mid = section.get("midterm") if isinstance(section.get("midterm"), Mapping) else {}
    months = section.get("months") if isinstance(section.get("months"), Mapping) else {}
    mid_months = mid.get("months") if isinstance(mid.get("months"), Mapping) else {}
    return {
        "month": _semi_stats(months.get(month), years),
        "q4": _semi_stats(section.get("q4"), years),
        "midterm": _semi_stats(mid_months.get(month), years, min_n=SEMI_MIN_MIDTERM_YEARS) if midterm else None,
        "midterm_q4": _semi_stats(mid.get("q4"), years, min_n=SEMI_MIN_MIDTERM_YEARS) if midterm else None,
        "pending": False,
        "range": {"first": section["first_month"], "last": section["last_month"]},
    }


def season_values(table: Mapping[str, Any], year: int, month: int) -> dict:
    """`season.now` 的 values：當月 S&P／加權指數／SOXX／SOX 的歷史統計；期中選舉年那幾列只在期中選舉年放
    （半導體的期中選舉年統計樣本不到 SEMI_MIN_MIDTERM_YEARS 年也不放）。"""

    def stats(section: Any, *path: str) -> dict | None:
        node = section
        for key in path:
            if not isinstance(node, Mapping) or key not in node:
                return None
            node = node[key]
        return dict(node) if isinstance(node, Mapping) else None

    midterm = is_midterm_year(year)
    sp = table.get("sp500") if isinstance(table, Mapping) else None
    tw = table.get("taiex") if isinstance(table, Mapping) else None
    tw_pending = not isinstance(tw, Mapping) or bool(tw.get("pending_full_data"))
    m = str(month)
    soxx = _semi_values(table.get("soxx") if isinstance(table, Mapping) else None, midterm, m)
    sox = _semi_values(table.get("sox") if isinstance(table, Mapping) else None, midterm, m)
    return {
        "month": month,
        "midterm_year": midterm,
        "sp500": stats(sp, "months", m),
        "sp500_q4": stats(sp, "q4"),
        "sp500_midterm": stats(sp, "midterm", "months", m) if midterm else None,
        "sp500_midterm_q4": stats(sp, "midterm", "q4") if midterm else None,
        "taiex": None if tw_pending else stats(tw, "months", m),
        "taiex_q4": None if tw_pending else stats(tw, "q4"),
        "taiex_midterm": None if tw_pending or not midterm else stats(tw, "midterm", "months", m),
        "taiex_pending": tw_pending,
        "soxx": soxx["month"],
        "soxx_q4": soxx["q4"],
        "soxx_midterm": soxx["midterm"],
        "soxx_midterm_q4": soxx["midterm_q4"],
        "soxx_pending": soxx["pending"],
        "soxx_range": soxx["range"],
        "sox": sox["month"],
        "sox_q4": sox["q4"],
        "sox_midterm": sox["midterm"],
        "sox_midterm_q4": sox["midterm_q4"],
        "sox_pending": sox["pending"],
        "sox_range": sox["range"],
    }


# ── 通膨：損益兩平（2026-10-24 批 2 C 軌；PLAN_r9_andrew9_v2 A3、A6、A8）──────────────
# 5 年／10 年損益兩平通膨＝財政部名目殖利率減 TIPS 實質殖利率，名目與實質同一天、同一天期才算。殖利率與損益兩平一律用
# 整數 bp 算差值與門檻：2 位小數的殖利率用 float 相減，剛好 5 bp 的變化有近一半會掉到「<0.05」（審查 r9 s3-F3）。
# 分類結果存成列舉碼，句子由 bot 查固定表（比照 contra.* → CONTRA_TEXT）；這裡不寫任何句子。
BE_CHANGE_DAYS = 5  # 「5 日變化」＝名目與實質的「共同日期」序列往前數 5 個（10/12、11/11 債市休、股市開：兩個檔都沒有那天）
# 最新與第 −6 個共同日期最多相差幾個日曆天（含）；超過就不算（2026-10-28 審查 seat3 nit2：實質檔缺兩週時「5 日變化」會跨 23 天、標 up）。
# 自然發生的最長是感恩節那週（週五→下週一，剛好 10 天），所以上限是 10、超過 10 才不算。
BE_CHANGE_MAX_SPAN_DAYS = 10
BE_MOVE_BP = 5  # 5 日變化 ≥5 bp（含剛好 5）才寫上升／下降，不到就是持平
BE_SPREAD_BP = 10  # 5 年減 10 年 ≥10 bp（含剛好 10）才寫「5 年高於 10 年」，≤−10 寫「低於」
BE_MOVE_CODES = ("up", "flat", "down")
BE_SPREAD_CODES = ("five_higher", "similar", "five_lower")
TEXT_BE_BASIS = "名目殖利率減實質殖利率（%）"


def _infl_dec(value: Any) -> Decimal | None:
    """數字（int／float／數字字串）→ Decimal。一律走 str()：Decimal(float) 會把二進位誤差帶進來。bool、NaN、無限大回 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    if isinstance(value, str) and not value.isascii():
        return None  # Decimal() 連阿拉伯數字等 Unicode 數字都收；來源給的字只收 ASCII
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def yield_bp(value: Any) -> int | None:
    """殖利率（%，財政部給 2 位小數）→ 整數 bp（4.78 → 478）；不是整數 bp（3 位小數以上）、超過 ±50% 或看不懂回 None。
    運算放在自己的 Decimal 精度裡（bot 也 import 這個模組，不碰全域的 decimal 設定，也不受它影響）。"""
    number = _infl_dec(value)
    if number is None:
        return None
    with localcontext(Context(prec=40)):
        scaled = number * 100
        if scaled != scaled.to_integral_value() or abs(scaled) > 5000:
            return None
        return int(scaled)


def _infl_day(value: Any) -> date | None:
    if isinstance(value, str) and _ISO_DATE_RE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def breakeven_series(rows: Sequence[Any], tenor: int) -> list[tuple[date, int]]:
    """`us.infl.be` 的列 {"date", "n5", "n10", "r5", "r10"}（日期＋名目與實質殖利率，%；具名鍵，之後加欄不會錯位）→ 該天期每個
    「名目與實質同一天都有值」的日期與損益兩平（bp），由舊到新。名目領先一天、實質缺格的日子不在裡面；同一天重複以後面的為準。"""
    if tenor not in (5, 10):
        raise ValueError("tenor must be 5 or 10")
    nominal_key, real_key = f"n{tenor}", f"r{tenor}"
    found: dict[date, int] = {}
    for row in rows if isinstance(rows, (list, tuple)) else ():
        if not isinstance(row, Mapping):
            continue
        day, nominal, real = _infl_day(row.get("date")), yield_bp(row.get(nominal_key)), yield_bp(row.get(real_key))
        if day is not None and nominal is not None and real is not None:
            found[day] = nominal - real
    return sorted(found.items())


def be_move_code(change_bp: int) -> str:
    """5 日變化（bp）→ up／flat／down；剛好 ±5 bp 算上升／下降。"""
    return "up" if change_bp >= BE_MOVE_BP else "down" if change_bp <= -BE_MOVE_BP else "flat"


def be_spread_code(spread_bp: int) -> str:
    """5 年減 10 年（bp）→ five_higher／five_lower／similar；剛好 ±10 bp 算高於／低於。"""
    return "five_higher" if spread_bp >= BE_SPREAD_BP else "five_lower" if spread_bp <= -BE_SPREAD_BP else "similar"


@dataclass(frozen=True)
class BeTenor:
    date: date  # 這個天期最新的「共同日期」
    bp: int  # 損益兩平（bp）
    chg5_bp: int | None  # 和 5 個共同日期前的差（bp）；共同日期不足 6 個是 None
    move: str | None  # be_move_code(chg5_bp)


def breakeven_tenor(series: Sequence[tuple[date, int]]) -> BeTenor | None:
    if not series:
        return None
    day, bp = series[-1]
    change = None
    if len(series) > BE_CHANGE_DAYS:
        base_day, base_bp = series[-1 - BE_CHANGE_DAYS]
        if (day - base_day).days <= BE_CHANGE_MAX_SPAN_DAYS:
            change = bp - base_bp
    return BeTenor(day, bp, change, be_move_code(change) if change is not None else None)


def breakeven_values(rows: Sequence[Any]) -> dict | None:
    """`us.infl.be` 的 values：兩個天期各自用自己的最新共同日期（實質缺格的天期不拖累另一個）；5 年減 10 年只在兩個天期同一天時算。
    兩個天期都算不出來回 None。"""
    tenors = {tenor: breakeven_tenor(breakeven_series(rows, tenor)) for tenor in (5, 10)}
    if tenors[5] is None and tenors[10] is None:
        return None

    def part(item: BeTenor | None) -> dict | None:
        if item is None:
            return None
        return {"date": item.date.isoformat(), "value": item.bp / 100, "chg5_bp": item.chg5_bp, "move": item.move}

    spread = None
    if tenors[5] is not None and tenors[10] is not None and tenors[5].date == tenors[10].date:
        spread = tenors[5].bp - tenors[10].bp
    return {
        "y5": part(tenors[5]),
        "y10": part(tenors[10]),
        "spread_bp": spread,
        "spread": be_spread_code(spread) if spread is not None else None,
    }


# ── 通膨：美國 CPI 與核心 CPI（BLS；2026-10-24 批 2 C 軌；PLAN_r9_andrew9_v2 A2–A5、A8） ──────────
# 年增用未季調指數、月增與近 3 月年化用季調指數（BLS v1 沒有 net／percent change，全部自己算）。年增、月增、近 3 月年化先用
# Decimal 的 ROUND_HALF_UP 取一位小數（BLS 的公布精度），所有門檻與差值都在這個顯示值上算，卡上印的就是同一個值：
# 沒四捨五入的 3.531 會判「明顯高於」、卡上卻寫 3.5（審查 r9 s3-F3）。規則（起始值，沒有回測，可調）：
#   R1 水準＝年增−2：≤0 不高於、≤0.5 略高於、≤1.5 高於、>1.5 明顯高於；R2 動能＝近 3 月年化−年增：≥+0.5 加速、≤−0.5 降溫；
#   R3 年增月變（和上個月的年增比）；R4 整體 vs 核心的年增差：≥+0.5／≤−0.5 才寫；R8 基期缺值（2025-10 政府停擺）：保留最近一個
#   算得出年增的月份並帶月份；R9 每個值帶自己的月份。結果存成列舉碼，句子由 bot 查固定表；PCE、密大、台灣 CPI 本版不做。
CPI_HOST = "api.bls.gov"  # 2026-10-24 Andrew 同意（v1 免註冊、每天 25 次）；通膨的來源、collect、outcome 共用這個名字
CPI_NSA, CPI_SA, CORE_NSA, CORE_SA = "CUUR0000SA0", "CUSR0000SA0", "CUUR0000SA0L1E", "CUSR0000SA0L1E"
CPI_SERIES = (CPI_NSA, CPI_SA, CORE_NSA, CORE_SA)  # 全項未季調、全項季調、核心（不含食品能源）未季調、核心季調
INFL_TARGET = Decimal("2.0")
INFL_LEVEL_STEPS = (Decimal("0"), Decimal("0.5"), Decimal("1.5"))  # R1：差 ≤0／≤0.5／≤1.5／>1.5
INFL_MOMENTUM_STEP = Decimal("0.5")  # R2
INFL_GAP_STEP = Decimal("0.5")  # R4
INFL_YOY_RANGE = (Decimal("-5"), Decimal("25"))  # R8：年增不在這個範圍就當暫缺
INFL_MOM_RANGE = (Decimal("-10"), Decimal("10"))  # 月增、近 3 月年化的合理範圍（資料壞掉才會超過）
INFL_ANN3_RANGE = (Decimal("-50"), Decimal("100"))
INFL_YOY_LOOKBACK = 24  # 最近一個算得出年增的月份最多往前找幾個月
INFL_CONTEXT = Context(prec=40, rounding=ROUND_HALF_UP)  # 運算用自己的精度與捨入（bot 也 import 這個模組，不碰全域設定）
INFL_LEVEL_CODES = ("not_above", "slightly_above", "above", "well_above")
INFL_MOMENTUM_CODES = ("accelerating", "steady", "cooling")
INFL_MOVE_CODES = ("up", "flat", "down")
INFL_GAP_CODES = ("headline_higher", "similar", "headline_lower")
TEXT_INFL_BASIS = "BLS 指數：年增用未季調，月增與年化用季調，數字由程式計算"

_YM_RE = re.compile(r"(\d{4})-(0[1-9]|1[0-2])", re.ASCII)


def month_shift(ym: str, n: int) -> str:
    """"YYYY-MM" 加減 n 個月（跨年）：month_shift("2026-01", -3) == "2025-10"。"""
    match = _YM_RE.fullmatch(ym) if isinstance(ym, str) else None
    if match is None:
        raise ValueError("bad month")
    year, month = divmod(int(match.group(1)) * 12 + int(match.group(2)) - 1 + n, 12)
    return f"{year:04d}-{month + 1:02d}"


def _month_index(ym: str) -> int:
    match = _YM_RE.fullmatch(ym)
    if match is None:
        raise ValueError("bad month")
    return int(match.group(1)) * 12 + int(match.group(2)) - 1


def _month_first(ym: str) -> str:
    return f"{ym}-01"


def _index_value(series: Any, ym: str) -> Decimal | None:
    value = _infl_dec(series.get(ym)) if isinstance(series, Mapping) else None
    return value if value is not None and value > 0 else None


def _round1(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _within(value: Decimal, limits: tuple[Decimal, Decimal]) -> Decimal | None:
    return value if limits[0] <= value <= limits[1] else None


def cpi_yoy(nsa: Any, ym: str) -> Decimal | None:
    """年增（%，未季調，一位小數）：要 ym 和 12 個月前都有值（2025-10 是缺值 → 2026-10 算不出）；不在 −5～25 當暫缺。"""
    now_value, base = _index_value(nsa, ym), _index_value(nsa, month_shift(ym, -12))
    if now_value is None or base is None:
        return None
    with localcontext(INFL_CONTEXT):
        return _within(_round1((now_value / base - 1) * 100), INFL_YOY_RANGE)


def cpi_mom(sa: Any, ym: str) -> Decimal | None:
    """月增（%，季調，一位小數）。"""
    now_value, before = _index_value(sa, ym), _index_value(sa, month_shift(ym, -1))
    if now_value is None or before is None:
        return None
    with localcontext(INFL_CONTEXT):
        return _within(_round1((now_value / before - 1) * 100), INFL_MOM_RANGE)


def cpi_ann3(sa: Any, ym: str) -> Decimal | None:
    """近 3 月年化（%，季調，一位小數）：(I_t／I_t−3)^4−1，t−3 跨年也照 (年, 月) 算。"""
    now_value, before = _index_value(sa, ym), _index_value(sa, month_shift(ym, -3))
    if now_value is None or before is None:
        return None
    with localcontext(INFL_CONTEXT):
        ratio = now_value / before
        squared = ratio * ratio
        return _within(_round1((squared * squared - 1) * 100), INFL_ANN3_RANGE)


def infl_level(yoy: Decimal) -> str:
    """R1：年增與 2% 的差（在一位小數的顯示值上算，上緣含等號）。"""
    with localcontext(INFL_CONTEXT):
        gap = yoy - INFL_TARGET
        low, mid, high = INFL_LEVEL_STEPS
        return "not_above" if gap <= low else "slightly_above" if gap <= mid else "above" if gap <= high else "well_above"


def infl_momentum(ann3: Decimal, yoy: Decimal) -> str:
    """R2：近 3 月年化減年增，剛好 ±0.5 算加速／降溫。"""
    with localcontext(INFL_CONTEXT):
        diff = ann3 - yoy
        return "accelerating" if diff >= INFL_MOMENTUM_STEP else "cooling" if diff <= -INFL_MOMENTUM_STEP else "steady"


def infl_move(yoy: Decimal, previous: Decimal) -> tuple[str, Decimal]:
    """R3：年增和上個月的年增比（一位小數的顯示值，差 0 才算持平）；回 (up／flat／down, 差的絕對值)。"""
    with localcontext(INFL_CONTEXT):
        diff = yoy - previous
        return ("up" if diff > 0 else "down" if diff < 0 else "flat"), abs(diff)


def infl_gap(headline: Decimal, core: Decimal) -> tuple[str, Decimal]:
    """R4：整體年增減核心年增，剛好 ±0.5 才寫；回 (代碼, 有號的差)。"""
    with localcontext(INFL_CONTEXT):
        diff = headline - core
        code = "headline_higher" if diff >= INFL_GAP_STEP else "headline_lower" if diff <= -INFL_GAP_STEP else "similar"
        return code, diff


def _cpi_latest(points: Any) -> str | None:
    """序列裡最大的 (年, 月)（有數字的月份才算；缺值不算前進）。"""
    if not isinstance(points, Mapping):
        return None
    months = [key for key, value in points.items()
              if isinstance(key, str) and _YM_RE.fullmatch(key) and _infl_dec(value) is not None]
    return max(months) if months else None


@dataclass(frozen=True)
class InflMeasure:
    """CPI 或核心 CPI 一組（月份都是 "YYYY-MM"）：month＝未季調指數的最新月；yoy 是最近一個算得出的年增（基期缺值時月份比 month
    早，R8）；mom、ann3 是季調最新月 sa_month 的；動能用同一個月（yoy_month）的近 3 月年化和年增相減。"""

    month: str
    yoy: Decimal | None
    yoy_month: str | None
    yoy_prev: Decimal | None
    sa_month: str | None
    mom: Decimal | None
    ann3: Decimal | None
    level: str | None
    momentum: str | None
    yoy_move: str | None
    yoy_move_pp: Decimal | None


def infl_measure(nsa: Any, sa: Any, aligned: bool) -> InflMeasure | None:
    month = _cpi_latest(nsa)
    if month is None:
        return None
    yoy_month, yoy = None, None
    cursor = month
    for _ in range(INFL_YOY_LOOKBACK):
        found = cpi_yoy(nsa, cursor)
        if found is not None:
            yoy_month, yoy = cursor, found
            break
        cursor = month_shift(cursor, -1)
    previous = cpi_yoy(nsa, month_shift(yoy_month, -1)) if yoy_month is not None else None
    sa_month = _cpi_latest(sa)
    ann3_then = cpi_ann3(sa, yoy_month) if yoy_month is not None else None
    move = infl_move(yoy, previous) if yoy is not None and previous is not None else (None, None)
    return InflMeasure(
        month=month, yoy=yoy, yoy_month=yoy_month, yoy_prev=previous, sa_month=sa_month,
        mom=cpi_mom(sa, sa_month) if sa_month is not None else None,
        ann3=cpi_ann3(sa, sa_month) if sa_month is not None else None,
        level=infl_level(yoy) if yoy is not None else None,
        # 動能和整體 vs 核心只在四個序列最新月份一樣時才算（不然會把不同月份相減）
        momentum=infl_momentum(ann3_then, yoy) if aligned and yoy is not None and ann3_then is not None else None,
        yoy_move=move[0], yoy_move_pp=move[1],
    )


def _float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _measure_values(item: InflMeasure) -> dict:
    return {
        "month": _month_first(item.month),
        "yoy": _float(item.yoy),
        "yoy_month": _month_first(item.yoy_month) if item.yoy_month else None,
        "yoy_prev": _float(item.yoy_prev),
        "mom": _float(item.mom),
        "ann3": _float(item.ann3),
        "sa_month": _month_first(item.sa_month) if item.sa_month else None,
        "level": item.level,
        "momentum": item.momentum,
        "yoy_move": item.yoy_move,
        "yoy_move_pp": _float(item.yoy_move_pp),
    }


def cpi_values(series: Any, retrieved: date) -> dict | None:
    """`us.infl.cpi` 的 values。series＝{序列代號: {"YYYY-MM": 指數}}（四個 CPI_SERIES）；retrieved＝取得日（BLS 條款要求註明）。
    全項未季調沒有資料回 None；四個序列最新月份不一樣時 aligned=False、動能與整體 vs 核心不算。"""
    if not isinstance(series, Mapping):
        return None
    nsa_h, sa_h, nsa_c, sa_c = (series.get(sid) for sid in CPI_SERIES)
    latest = [_cpi_latest(points) for points in (nsa_h, sa_h, nsa_c, sa_c)]
    aligned = None not in latest and len(set(latest)) == 1
    headline = infl_measure(nsa_h, sa_h, aligned)
    if headline is None:
        return None
    core = infl_measure(nsa_c, sa_c, aligned)
    gap = gap_pp = None
    if aligned and core is not None and headline.yoy is not None and core.yoy is not None \
            and headline.yoy_month == core.yoy_month:
        gap, difference = infl_gap(headline.yoy, core.yoy)
        gap_pp = float(difference)  # 有號：整體減核心（一位小數）
    return {
        "cpi": _measure_values(headline),
        "core": _measure_values(core) if core is not None else None,
        "aligned": aligned,
        "gap": gap,
        "gap_pp": gap_pp,
        "retrieved": retrieved.isoformat(),
    }


def cpi_stored_month(series: Any) -> str | None:
    """已存的四個序列「都有」的最新月份（最舊的那個序列的最新月）；缺任何一個序列或沒有有效月份回 None（當冷啟動）。"""
    if not isinstance(series, Mapping):
        return None
    latest = [_cpi_latest(series.get(sid)) for sid in CPI_SERIES]
    return None if None in latest else min(latest)


# BLS 輪詢（collect 與 job 的 outcome 共用同一份）：期間＝「現在應該已公布的最新一月」（比照 _cftc_expected）。CPI 在每月 10–16 號
# 美東 08:30 公布，所以每月 8 號起期待上個月的資料、8–20 號每 2 小時問一次直到最新月份前進；只在平日美東 08:30–20:00 問（公布
# 時刻是 08:30，每天 ≤6 次，加上月份前進後另外 3 個序列也在 HostRule(5.0, 12)（台北日計）之內）。冷啟動（沒資料）或落後超過
# 一個月不受日期、時段限制，立刻補抓；補抓沒進展時最多每天一次。窗口過了還沒更新（延後公布）也是每個平日一次，不放棄。
CPI_POLL_FIRST_DAY, CPI_POLL_LAST_DAY = 8, 20
CPI_POLL_FROM_ET, CPI_POLL_UNTIL_ET = time(8, 30), time(20, 0)
CPI_POLL_EVERY_S = 2 * 3600.0
CPI_PROBE_EVERY_S = 24 * 3600.0
CPI_POLL_STATES = ("disabled", "done", "pending", "exhausted")


def cpi_expected_month(day: date) -> str:
    """美東 `day` 這天應該已公布的最新一月：8 號起是上個月、之前是上上個月。"""
    return month_shift(f"{day.year:04d}-{day.month:02d}", -1 if day.day >= CPI_POLL_FIRST_DAY else -2)


CPI_FUTURE_SLACK_MONTHS = 1  # 比「應已公布的最新月」最多再多 1 個月（BLS 提早公布、日界線附近的容錯）；再晚的月份是來源出狀況


def cpi_month_ceiling(expected: str) -> str:
    """BLS 的資料最晚可能到哪個月：「應已公布的最新月」（cpi_expected_month）再加 CPI_FUTURE_SLACK_MONTHS 個月。

    2026-10-28 審查 gp2 C1：BLS 的某一次 200 回應含 2099-12 這種未來月份，以前照收 → 之後每輪 conflict:regress、CPI 永久凍結，
    publish 還發出一列要到 2100 年才過期的資料。所以 collect 拒收更晚的月份（ParseError("date", "future")）、已存的更晚月份
    當成毒化資料洗掉（cpi_trim_future），publish 看到就讓這一列暫缺。"""
    return month_shift(expected, CPI_FUTURE_SLACK_MONTHS)


def cpi_trim_future(series: Any, ceiling: str) -> tuple[dict, bool]:
    """{序列代號: {"YYYY-MM": 指數}} → (去掉晚於 ceiling 的月份後的複本, 有沒有去掉東西)。

    不改輸入；不是月份的鍵原樣留著（別的程式本來就忽略它們）；不是 Mapping 的序列原樣留著；整個輸入不是 Mapping 回 ({}, False)。"""
    if not isinstance(series, Mapping):
        return {}, False
    trimmed: dict = {}
    dropped = False
    for series_id, points in series.items():
        if isinstance(points, Mapping):
            keep = {key: value for key, value in points.items()
                    if not (isinstance(key, str) and _YM_RE.fullmatch(key) and key > ceiling)}
            dropped = dropped or len(keep) != len(points)
            trimmed[series_id] = keep
        else:
            trimmed[series_id] = points
    return trimmed, dropped


def cpi_lag_months(stored: str | None, expected: str) -> int | None:
    """已存最新月份落後 expected 幾個月（沒落後或比它新是 0）；沒有資料 None。"""
    if stored is None:
        return None
    return max(0, _month_index(expected) - _month_index(stored))


@dataclass(frozen=True)
class CpiPoll:
    state: str  # disabled（主機不在白名單）、done（已有期待的月份）、pending（還在等／補抓）、exhausted（窗口過了還沒更新，每個平日問一次）
    due: bool  # 現在該不該去問 BLS 一次
    expected: str  # 現在應該已公布的最新一月 "YYYY-MM"

    @property
    def ceiling(self) -> str:
        """collect 收得下的最晚月份（見 cpi_month_ceiling）。"""
        return cpi_month_ceiling(self.expected)


def cpi_poll(now_et: datetime, stored: str | None, last_probe: float | None, *, enabled: bool) -> CpiPoll:
    """now_et＝美東時間（aware）；stored＝cpi_stored_month 的結果；last_probe＝上一次問了沒進展的 epoch 秒（沒有是 None）。"""
    expected = cpi_expected_month(now_et.date())
    if not enabled:
        return CpiPoll("disabled", False, expected)
    if stored is not None and stored >= expected:
        return CpiPoll("done", False, expected)
    now = now_et.timestamp()
    in_window = CPI_POLL_FIRST_DAY <= now_et.day <= CPI_POLL_LAST_DAY
    catch_up = stored is None or _month_index(expected) - _month_index(stored) >= 2

    def spaced(gap: float) -> bool:
        # last_probe 記在未來（寫入時系統時鐘曾往前跳）：當成沒問過，不然會一直 not due 直到時鐘追上（2026-10-28 審查 gp2 N4）
        return last_probe is None or last_probe > now or now - last_probe >= gap

    state = "pending" if in_window or catch_up else "exhausted"
    if catch_up and spaced(CPI_PROBE_EVERY_S):
        return CpiPoll(state, True, expected)
    if now_et.weekday() >= 5 or not CPI_POLL_FROM_ET <= now_et.time() < CPI_POLL_UNTIL_ET:
        return CpiPoll(state, False, expected)
    return CpiPoll(state, spaced(CPI_POLL_EVERY_S if in_window else CPI_PROBE_EVERY_S), expected)


# ── 顯示用快照的列 id 與 values 欄位（job 寫、bot 讀，兩端 import 這一份） ────────────
# 型別：num（int 或 float、有限、不是 bool）、int、str、bool、date（"YYYY-MM-DD"）、any；後綴「?」＝可為 null；
# dict＝固定鍵（全部要有）、{"*": spec}＝任意鍵、[spec]＝清單；("?", spec)＝可為 null 的巢狀結構；
# frozenset＝列舉碼（只收裡面的字串；可為 null 的寫成 ("?", frozenset(…))，2026-10-24 通膨列開始用）。
MA_DIST_SPEC = {str(n): "num?" for n in MA_WINDOWS}
STATS_SPEC = {"up_pct": "num", "median": "num", "mean": "num", "n": "int"}
SEASON_RANGE_SPEC = {"first": "str", "last": "str"}  # 樣本的第一個與最後一個月，"YYYY-MM"（market_seasonality.json 的 first_month／last_month）
PER_N_SPEC = {
    "above": "int",
    "below": "int",
    "equal": "int",
    "new_below": "int",
    "new_above": "int",
    "valid": "int",
    "total": "int",
    "prev_pct": "num?",
    "prev_valid": "int",
    "same_set_pct": "num?",
    "status": "str",
}
ETF_SPEC = {
    "close": "num",
    "chg_pct": "num?",
    "ma_dist": MA_DIST_SPEC,
    "above_ma20": "bool?",
    "dd240_pct": "num?",
    "rsi14": "num?",
    "vol_ratio": "num?",
    "vol_pct250": "num?",
    "vol_label": "str?",
    "pv_label": "str?",
    "vol_flag": "str?",
    "vol_note": "str?",
}
BREADTH_SPEC = {
    "per_n": {str(n): PER_N_SPEC for n in MA_WINDOWS},
    "adv": "int?",
    "dec": "int?",
    "flat": "int?",
    "new_high": "int?",
    "new_low": "int?",
    "universe_label": "str",
    "coverage_note": "str?",
}
GEX_SPEC = {
    "status": "str",
    "at_spot": "num?",
    "sign": "int?",
    "crossings": [{"price": "num", "direction": "str"}],
    "near_zero": "bool?",
    "coverage": "num?",
    "lower_bound": "bool",
    "rate_assumed": "bool",
}
OPTIONS_SPEC = {
    "spot": "num",
    "call_wall": "num?",
    "put_wall": "num?",
    "call_wall_dist_pct": "num?",
    "put_wall_dist_pct": "num?",
    # 牆距的基準（2026-10-24 第 9 輪 I1，只增不改）："close"＝距離對 ref_close 算，"spot"＝對現價算（ref_close 此時是 null）。
    # spec 沒有列舉型別：dist_basis ∈ DIST_BASES、兩個欄位互相對得上，由 market_publish 的 _options_basis_ok 檢查
    "ref_close": "num?",
    "dist_basis": "str",
    "wall_status": "str",
    "monthly": ("?", {"expiry": "date", "call_wall": "num?", "put_wall": "num?", "max_pain": "num?", "status": "str"}),
    "pcr_oi": "num?",
    "pcr_vol": "num?",
    "iv30": "num?",
    "gex": GEX_SPEC,
    "counts": {"raw": "int", "bad": "int", "conflict": "int", "oi_valid": "int", "gamma_valid": "int", "iv_replaced": "int"},
    "oi_date_note": "str",
}
# 損益兩平（breakeven_values 的輸出）：兩個天期各自帶自己的日期；move／spread 是列舉碼，句子由 bot 查固定表
BE_TENOR_SPEC = {"date": "date", "value": "num", "chg5_bp": "int?", "move": ("?", frozenset(BE_MOVE_CODES))}
BE_SPEC = {
    "y5": ("?", BE_TENOR_SPEC),
    "y10": ("?", BE_TENOR_SPEC),
    "spread_bp": "int?",
    "spread": ("?", frozenset(BE_SPREAD_CODES)),
}
# CPI（cpi_values 的輸出）：月份一律是該月 1 日的 ISO 日期（R9：每個值帶自己的月份）；level／momentum／yoy_move／gap 是列舉碼；
# retrieved＝資料取得日（BLS 條款要求註明；卡片寫「取得日」）。
INFL_MEASURE_SPEC = {
    "month": "date",
    "yoy": "num?",
    "yoy_month": "date?",
    "yoy_prev": "num?",
    "mom": "num?",
    "ann3": "num?",
    "sa_month": "date?",
    "level": ("?", frozenset(INFL_LEVEL_CODES)),
    "momentum": ("?", frozenset(INFL_MOMENTUM_CODES)),
    "yoy_move": ("?", frozenset(INFL_MOVE_CODES)),
    "yoy_move_pp": "num?",
}
INFL_SPEC = {
    "cpi": INFL_MEASURE_SPEC,
    "core": ("?", INFL_MEASURE_SPEC),
    "aligned": "bool",
    "gap": ("?", frozenset(INFL_GAP_CODES)),
    "gap_pp": "num?",
    "retrieved": "date",
}
# macro.TACO（2026-10-24）：MacroMargin「川普壓力指數」。value＝來源 index.value；chg20＝來源 index.change20d（20 個交易日前
# 的指數點數差）；percentile＝歷史裡 ≤ 最後一筆的天數占比（0–100 整數，四捨五入）；n、since＝歷史天數與第一天；
# max／min 與各自的日期；status_key＝來源 index.status.key（只收 [a-z_]{1,16}）；method＝"v1"（來源的方法版本等於
# TACO_METHOD_V1）或 "other"。只存數字、日期與兩個短列舉，不存來源的任何文字。
TACO_METHOD_V1 = "persistent-pressure-new4-v1"
TACO_MIN_DAYS = 60  # 歷史至少幾天才算位階
TACO_VALUE_MAX = 100.0  # |指數值| 上限（實際約 −5～+17）；超過代表頁面壞了
TACO_CHANGE_MAX = 200.0  # |20 日變化| 上限（兩個指數值之差）
TACO_LAST_ROW_TOLERANCE = 0.01  # 歷史最後一筆與 index.value 最多差多少（來源自己四捨五入）
TACO_CHANGE_DAYS = 20  # change20d＝現值減 20 個交易日前（歷史倒數第 21 筆）
TACO_CHANGE_TOLERANCE = 0.1  # change20d 與 (現值 − 歷史倒數第 21 筆) 最多差多少（來源的 change20d 只給一位小數）
TACO_METHODS = ("v1", "other")
TACO_SPEC = {
    "value": "num",
    "chg20": "num?",
    "percentile": "int",
    "n": "int",
    "since": "date",
    "max": "num",
    "max_date": "date",
    "min": "num",
    "min_date": "date",
    "status_key": "str?",
    "method": "str",
}
_ITEMS = "items"
ROW_IDS: dict[str, dict[str, Any]] = {
    **{f"us.etf.{s}": ETF_SPEC for s in ("VOO", "QQQ", "SOXX", "SPY")},
    **{f"us.breadth.{s}": BREADTH_SPEC for s in ("VOO", "QQQ", "SOXX")},
    **{f"us.options.{s}": OPTIONS_SPEC for s in ("QQQ", "SPY", "SOXX")},
    **{
        f"us.cftc.{s}": {
            "contract": "str",
            "lev_net": "int",
            "lev_net_chg": "int?",
            "am_net": "int",
            "am_net_chg": "int?",
            "oi": "int?",
        }
        for s in ("es", "nq")
    },
    "us.margin": {"month": "str", "debit_musd": "num", "yoy_pct": "num?"},
    "us.infl.be": BE_SPEC,
    "us.infl.cpi": INFL_SPEC,
    **{
        f"us.13f.{s}": {
            "rows": [
                {
                    "name": "str",
                    "symbols": ["str"],
                    "weight": "num?",
                    "increased": "int?",
                    "decreased": "int?",
                    "new": "int?",
                    "soldout": "int?",
                    "own_pct": "num?",
                }
            ],
            "summary": {"more_increase": "int", "missing": "int", "total": "int"},
            "holder_period_note": "str?",
            "weight_basis": "str",
        }
        for s in ("VOO", "QQQ", "SOXX")
    },
    **{
        f"us.fund.{s}": {
            "rows": [{"name": "str", "symbols": ["str"], "pe": "num?", "status": "str"}],
            "pe_median": "num?",
            "pe_valid": "int",
            "total": "int",
        }
        for s in ("VOO", "QQQ", "SOXX")
    },
    # rsi14（2026-10-24 第 9 輪 A18）：一律有這個鍵，窗口有缺口算不出來就是 null；舊快照沒有這個鍵，bot 當沒有
    "tw.taiex": {"close": "num", "chg": "num?", "chg_pct": "num?", "ma_dist": MA_DIST_SPEC, "above_ma20": "bool?", "dd240_pct": "num?", "rsi14": "num?"},
    "tw.etf.0050": ETF_SPEC,
    "tw.breadth.0050": BREADTH_SPEC,
    "tw.ad": {"adv": "int", "dec": "int", "flat": "int", "adv_limit": "int?", "dec_limit": "int?", "no_compare": "int?"},
    "tw.turnover": {"value_twd": "num", "vol_ratio": "num?", "vol_pct250": "num?", "vol_label": "str?", "vol_flag": "str?"},
    "tw.inst3": {
        "foreign_yi": "num",
        "trust_yi": "num",
        "dealer_yi": "num",
        "foreign_5d_yi": "num?",
        "trust_5d_yi": "num?",
        "dealer_5d_yi": "num?",
        "days_5d": "int",
    },
    "tw.margin": {"balance_yi": "num", "change_yi": "num?"},
    "tw.futures": {"foreign_net_big": "int", "foreign_net_big_chg": "int?", "foreign_net_mini": "int?", "foreign_net_micro": "int?"},
    "tw.options": {
        "month": "str",
        "spot": "num?",
        "call_wall": "num?",
        "put_wall": "num?",
        "max_pain": "num?",
        "call_wall_dist_pct": "num?",
        "put_wall_dist_pct": "num?",
        "status": "str",
        "pcr_oi_pct": "num?",
        "foreign_call_net": "int?",
        "foreign_put_net": "int?",
    },
    "tw.fund.0050": {"pe_median": "num?", "pe_valid": "int", "total": "int", "rev_month": "str?", "rev_pos": "int", "rev_valid": "int"},
    **{
        f"macro.{s}": {"value": "num", "chg": "num?", "chg_pct": "num?", "source": "str"}
        for s in (
            "VIX", "VIX3M", "UST3M", "UST10Y", "DXY", "WTI", "BRENT", "GOLD", "SILVER", "BTC", "USDTWD",
            "RUT", "DJI", "IXIC", "MOVE", "SPY_DD",
            # 2026-10-24 第 9 輪（A12，審查 r9 s3-F9／s4-F13）：CBOE 的 VXTLT（TLT 選擇權波動，約 15）是獨立的一列，
            # 不寫進 MOVE（約 100，量綱差 6–7 倍；MOVE 恢復時兩列並存）。只增不改：既有列的語意、型別、單位都沒變，
            # 舊 bot 讀新快照時只認得 READ_ROWS，新列直接略過；stale 規則等 collect 時用 KIND_US_DAILY（同 VIX）。
            "VXTLT",
        )
    },
    # 2026-10-24 第 9 輪批 1（A27，審查 r9b gp2 N4）：SPY 價格代理的距高點用到 CBOE 暫定收盤時 provisional＝true（一律有這個鍵，
    # false＝正式收盤）。下面這個鍵蓋掉上面共用的形狀、列的先後順序不變；舊快照沒有這個鍵，bot 當 false。
    "macro.SPY_DD": {"value": "num", "chg": "num?", "chg_pct": "num?", "source": "str", "provisional": "bool"},
    "macro.TACO": TACO_SPEC,
    "events.next": {_ITEMS: [{"name": "str", "at": "num", "kind": "str"}]},
    "season.now": {
        "month": "int",
        "midterm_year": "bool",
        "sp500": ("?", STATS_SPEC),
        "sp500_q4": ("?", STATS_SPEC),
        "sp500_midterm": ("?", STATS_SPEC),
        "sp500_midterm_q4": ("?", STATS_SPEC),
        "taiex": ("?", STATS_SPEC),
        "taiex_q4": ("?", STATS_SPEC),
        "taiex_midterm": ("?", STATS_SPEC),
        "taiex_pending": "bool",
        # 2026-10-24 第 9 輪 A14（只增不改）：SOXX 自身與 SOX 長歷史；舊 bot 讀新快照時略過這些鍵，新 bot 讀舊快照時當 pending
        "soxx": ("?", STATS_SPEC),
        "soxx_q4": ("?", STATS_SPEC),
        "soxx_midterm": ("?", STATS_SPEC),
        "soxx_midterm_q4": ("?", STATS_SPEC),
        "soxx_pending": "bool",
        "soxx_range": ("?", SEASON_RANGE_SPEC),
        "sox": ("?", STATS_SPEC),
        "sox_q4": ("?", STATS_SPEC),
        "sox_midterm": ("?", STATS_SPEC),
        "sox_midterm_q4": ("?", STATS_SPEC),
        "sox_pending": "bool",
        "sox_range": ("?", SEASON_RANGE_SPEC),
    },
    "election": {
        "vote_date": "date",
        "days_left": "int?",
        "state": "str",
        "quotes": [
            {"platform": "str", "chamber": "str", "outcome": "str", "mid": "num?", "last": "num?", "updated_utc": "num?"}
        ],
    },
    "trump.summary": {"topics": {"*": "int"}, "latest_utc": "num?", "at_least": "bool"},
    **{f"news.{s}": {_ITEMS: [{"title": "str", "source": "str?", "published_utc": "num"}]} for s in ("us", "tw", "iran", "semi")},
    "trump.posts": {_ITEMS: [{"excerpt": "str", "published_utc": "num", "quoted_author": "str?", "topics": ["str"]}]},
    "oneliner.us": {"text": "str?"},
    "oneliner.tw": {"text": "str?"},
    "contra.us": {"codes": ["str"], "text": "str?", "provisional": "bool"},  # provisional：命中的矛盾用到暫定收盤（A27）
    "contra.tw": {"codes": ["str"], "text": "str?"},
}
CHART_KEYS = ("QQQ", "SPY", "SOXX", "TXO")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)  # re.ASCII：\d 預設連阿拉伯數字、全形數字都收（2026-10-28 審查 gp2 N9）


def _base_ok(base: str, value: Any) -> bool:
    if base == "num":
        return _is_num(value)
    if base == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if base == "str":
        return isinstance(value, str)
    if base == "bool":
        return isinstance(value, bool)
    if base == "date":
        if not (isinstance(value, str) and _ISO_DATE_RE.fullmatch(value)):
            return False
        try:
            date.fromisoformat(value)
        except ValueError:
            return False
        return True
    if base == "any":
        return True
    raise ValueError(f"unknown spec {base!r}")


def spec_ok(spec: Any, value: Any) -> bool:
    """依上面的欄位型別表檢查一個值（bot 讀快照時型別錯 → 那格暫缺；job 寫快照前自我驗）。"""
    if isinstance(spec, str):
        if value is None:
            return spec.endswith("?")
        return _base_ok(spec.rstrip("?"), value)
    if isinstance(spec, tuple) and len(spec) == 2 and spec[0] == "?":
        return value is None or spec_ok(spec[1], value)
    if isinstance(spec, frozenset):
        return isinstance(value, str) and value in spec
    if isinstance(spec, list):
        return isinstance(value, list) and all(spec_ok(spec[0], item) for item in value)
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            return False
        if "*" in spec:
            return all(isinstance(k, str) and spec_ok(spec["*"], v) for k, v in value.items())
        return all(key in value and spec_ok(sub, value[key]) for key, sub in spec.items())
    raise ValueError("bad spec")


def values_ok(row_id: str, values: Any) -> bool:
    spec = ROW_IDS.get(row_id)
    return spec is not None and spec_ok(spec, values)


_TACO_STATUS_KEY_RE = re.compile(r"[a-z_]{1,16}")


def taco_stats(history: Sequence[tuple[date, float]]) -> dict:
    """川普壓力指數的歷史位階（2026-10-24）。history＝[(日期, 值), …]，日期嚴格遞增、至少 TACO_MIN_DAYS 筆。

    percentile＝歷史裡「≤ 最後一筆」的天數占比 ×100，四捨五入成整數（Decimal ROUND_HALF_UP：88.07 → 88、x.5 → 進位）；
    最後一筆自己也算（所以分子至少是 1，目前點是歷史最高時 100）。目前點是歷史最低時分子就是 1：1/n 先四捨五入再取整數，
    n 超過 200 時 1/n×100 小於 0.5，結果是「第 0 百分位」（例：n＝394 時 0.25 → 0；n＝200 時剛好 0.5 → 1）；2026-10-28 審查
    seat3 nit3：以前這裡把下限寫成 1/n（未取整），會讓人以為百分位至少是 1。比較用最後一筆（歷史自己的最新點），不用 index.value：
    兩者最多差 TACO_LAST_ROW_TOLERANCE，用 index.value 比的話最後一筆可能不算進自己的位階。
    最高／最低同值時取最早那天。"""
    rows = list(history)
    if len(rows) < TACO_MIN_DAYS:
        raise ValueError("history too short")
    current = rows[-1][1]
    at_or_below = sum(1 for _day, value in rows if value <= current)
    percentile = int((Decimal(at_or_below) * 100 / Decimal(len(rows))).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    high = max(rows, key=lambda row: row[1])  # max／min 遇到同值回第一個＝最早那天
    low = min(rows, key=lambda row: row[1])
    return {
        "percentile": percentile,
        "n": len(rows),
        "since": rows[0][0].isoformat(),
        "max": high[1],
        "max_date": high[0].isoformat(),
        "min": low[1],
        "min_date": low[0].isoformat(),
    }


def taco_values_ok(values: Any) -> bool:
    """macro.TACO 的 values：型別（TACO_SPEC）加上語意（範圍、百分位 0–100、天數、日期先後、最高最低包得住現值）。

    publish 組列時（不合就只讓這一列暫缺，不擋整份快照）與 bot 讀快照時（不合就當暫缺）共用；ROW_IDS 本身只管型別。"""
    if not spec_ok(TACO_SPEC, values):
        return False
    limit, tol = TACO_VALUE_MAX, TACO_LAST_ROW_TOLERANCE + 1e-9  # 浮點誤差：最高／最低 ±0.01 剛好算在內
    if not (abs(values["value"]) <= limit and abs(values["max"]) <= limit and abs(values["min"]) <= limit):
        return False
    change = values["chg20"]
    if change is not None and abs(change) > TACO_CHANGE_MAX:
        return False
    if not 0 <= values["percentile"] <= 100 or not TACO_MIN_DAYS <= values["n"] <= 20000:
        return False
    if not (values["min"] - tol <= values["value"] <= values["max"] + tol) or values["min"] > values["max"]:
        return False
    since = date.fromisoformat(values["since"])  # spec_ok 已確認是合法的 YYYY-MM-DD
    if since > date.fromisoformat(values["max_date"]) or since > date.fromisoformat(values["min_date"]):
        return False
    key = values["status_key"]
    if key is not None and not _TACO_STATUS_KEY_RE.fullmatch(key):
        return False
    return values["method"] in TACO_METHODS


ROW_ENVELOPE_SPEC = {
    "values": "any",
    "data_date": "date?",
    "period": "str?",
    "source": "str",
    "basis": "str?",
    "stale_after": "num",
    "state": "str",
}


def row_ok(row_id: str, row: Any) -> bool:
    """一整列（values＋資料日、來源、stale_after、state）；state=never 時 values 可以是空的。"""
    if not spec_ok(ROW_ENVELOPE_SPEC, row) or row["state"] not in ROW_STATES:
        return False
    if "label" in row and not isinstance(row["label"], str):
        return False
    if row["state"] == "never":
        return isinstance(row["values"], dict)
    return values_ok(row_id, row["values"])


def etf_values(tech: Technicals, volume: VolumeState) -> dict:
    """組 `us.etf.<SYM>`／`tw.etf.0050` 的 values。"""
    return {
        "close": tech.close,
        "chg_pct": tech.chg_pct,
        "ma_dist": {str(n): tech.ma_dist.get(n) for n in MA_WINDOWS},
        "above_ma20": tech.above.get(20),
        "dd240_pct": tech.dd240_pct,
        "rsi14": tech.rsi14,
        "vol_ratio": volume.ratio,
        "vol_pct250": volume.pct250,
        "vol_label": volume.label,
        "pv_label": volume.pv_label,
        "vol_flag": volume.day_flag,
        "vol_note": volume.note,
    }


def breadth_values(
    per_n: Mapping[int, Breadth],
    ad: AdCounts | None,
    highs_lows: HighsLows | None,
    *,
    universe_label: str,
    coverage_note: str | None = None,
) -> dict:
    """組 `us.breadth.<SYM>`／`tw.breadth.0050` 的 values（台股漲跌家數用證交所漲跌欄，傳進來的 ad 自己組）。"""
    return {
        "per_n": {
            str(n): {
                "above": b.above,
                "below": b.below,
                "equal": b.equal,
                "new_below": b.new_below,
                "new_above": b.new_above,
                "valid": b.valid,
                "total": b.total,
                "prev_pct": b.prev_pct,
                "prev_valid": b.prev_valid,
                "same_set_pct": b.same_set_pct,
                "status": b.status,
            }
            for n, b in ((n, per_n[n]) for n in MA_WINDOWS)
        },
        "adv": ad.adv if ad else None,
        "dec": ad.dec if ad else None,
        "flat": ad.flat if ad else None,
        "new_high": highs_lows.new_high if highs_lows else None,
        "new_low": highs_lows.new_low if highs_lows else None,
        "universe_label": universe_label,
        "coverage_note": coverage_note,
    }


# 美國經濟事件的卡片標籤（PLAN §6：先中文、括號英文；每個都 ≤24 字）。鍵是 market_sources_us 白名單裡的英文名，
# 不是來源原字串；卡片程式（market_dashboard）直接查這份，不過 clean_external（NFKC 會把全形括號變半形）。
US_EVENT_LABELS: dict[str, str] = {
    "CPI": "消費者物價（CPI）",
    "Core CPI": "核心消費者物價（Core CPI）",
    "PPI": "生產者物價（PPI）",
    "Core PPI": "核心生產者物價（Core PPI）",
    "Nonfarm Payrolls": "非農就業（NFP）",
    "Unemployment Rate": "失業率（Unemployment）",
    "GDP": "經濟成長率（GDP）",
    "PCE Price Index": "個人消費支出物價（PCE）",
    "Core PCE Price Index": "核心 PCE 物價（Core PCE）",
    "Retail Sales": "零售銷售（Retail Sales）",
    "Core Retail Sales": "核心零售銷售（Core Retail）",
    "ISM Manufacturing PMI": "製造業採購經理人指數（ISM）",
    "ISM Services PMI": "服務業採購經理人指數（ISM）",
    "Initial Jobless Claims": "初領失業金（Jobless Claims）",
    "JOLTS Job Openings": "職缺數（JOLTS）",
    "Michigan Consumer Sentiment": "密大消費者信心（Michigan）",
    "FOMC Minutes": "聯準會會議紀錄（FOMC）",
    "Fed Chair Speaks": "聯準會主席談話（Fed Chair）",
}


# 川普貼文主題的先後（和 market_sources_misc 的主題表同一順序，test_market_sources_misc 檢查）：卡上同票時照這個排，
# 快照是 sort_keys 寫的、保不住 dict 順序（2026-10-18：照字碼排會讓「關稅」掉出摘要的前 3 名）
TRUMP_TOPIC_ORDER = ("關稅", "伊朗", "聯準會", "利率", "油價", "中國", "以色列", "停火", "台灣", "半導體", "股市", "北約", "俄烏")


# 卡片、altText 會用到的全部固定句（測試用：禁用字、validator、年份）
FIXED_TEXTS = tuple(
    value for name, value in sorted(globals().items()) if name.startswith("TEXT_") and isinstance(value, str)
) + tuple(CONTRA_TEXT.values()) + tuple(VOL_PHRASE.values()) + tuple(GEX_DIRECTION_TEXT.values()) + VOL_LABELS + PV_LABELS + (FLAG_SETTLEMENT, FLAG_EARLY_CLOSE) + tuple(US_EVENT_LABELS.values())
