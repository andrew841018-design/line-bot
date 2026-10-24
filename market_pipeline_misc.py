"""市場盤勢（/盤勢，2026-10-17）雜項的 collect 任務：宏觀（Yahoo 為主、失敗改代理，BTC 也在裡面）、新聞、
川普原文、期中選舉預測市場（2026-10-23 起台灣封鎖，不抓）。

Andrew 2026-10-17 的要求（DECISIONS.md）：宏觀「有 yahoo 用 yahoo，沒有的情況改代理」；川普／伊朗事件、
季節性、期中選舉「你再試試看，truth social or....，我覺得可以做到」——能程式化的都做進來。這個模組只負責
「到期就抓、寫進 Store」，計算與顯示在 market_publish／market_dashboard。

規則（PLAN §3.1、§3.3、§9、§18.8、§18.9、§18.14、§19.1、§19.2；INTERFACES §3 C、§9）：
- 宏觀：D＝最近一個美東收盤＋30 分鐘已過的交易日；Yahoo spark 每個 D 只打 1 次（不重打）。Yahoo 失敗、
  429、冷卻 → 代理：美元 UUP、WTI USO、Brent BNO、金 GLD、銀 SLV、BTC IBIT（現貨比特幣 ETF）、羅素 2000 IWM、
  道瓊 DIA、Nasdaq 綜合 ONEQ（Nasdaq ETF 日線）、台幣＝期交所 USD/NTD（market_pipeline_tw 每天抓的 `tw.fx`，
  沒有才自己抓；標「代理：期交所匯率」）；MOVE 只有 Yahoo（失敗就記失敗、留著舊值）。Yahoo 只少了某幾個代號時，
  只有那幾個改代理。每格存實際來源字樣（「Yahoo」「ETF 代理：UUP」「期交所匯率」）。所有日線丟掉晚於 D 的列。
  日變化只在前一筆的日期＝前一個交易日時才算（§18.8）。
  ETF 代理（2026-10-23）要等 Nasdaq 日線端點公布 D 才去抓：SPY 在 closes 表有 D 的正式列（market_pipeline_us
  一直在追它，不算 CBOE 的 provisional）之後，每個代理抓一次；還沒有 D 那根就每小時再抓，最多再 3 次。原本 D 換日
  就開打、30 分鐘後放棄，但 Nasdaq 要幾個小時後才公布 D，代理整天落後一天。台幣（期交所匯率）：tw.fx 的資料日比
  卡上的台幣新時隨時重算，不必等下一個 D。
  BTC（2026-10-23）和其他格一樣走這條路、存 `macro.BTC`：原本 Coinbase 每 2 小時那條（§18.9、macro.btc 任務）
  拿掉了，因為 api.coinbase.com 在台灣被封鎖。
- 新聞：Google News RSS 美股、台股每 30 分鐘，伊朗每小時；pubDate 超過 36 小時、解析不到或在未來的不列；
  news.google.com 回 503 也算限流（market_http 會讓該主機冷卻 2 小時），冷卻期間不排。
- 川普原文：trumpstruth RSS 每 30 分鐘，依 originalId 累積進 Store（移除的也寫回去，之前累積到的那則才
  會被拿掉），從累積資料算近 48 小時的主題則數；這輪最舊一則比上輪最新一則還新（中間有缺口）或剛上線、
  涵蓋不到 48 小時 → 標「（至少）」（§18.14）。最舊與最新一則套和 parse 一樣的時間界線（2026-10-23）：
  未來的 item 不算、比視窗舊的只當「這份 feed 涵蓋到視窗之前」。
- 期中選舉：market_sources_misc.PREDICTION_MARKETS 裡固定的市場（Polymarket、Kalshi 的眾院與參院）各一個任務、
  每 2 小時；結果出來後每天看一次、顯示 7 天後不再抓。2026-10-23 起台灣封鎖這兩個網站：
  market_sources_misc.PREDICTION_MARKETS_BLOCKED 為真時這些任務永遠不到期（一個請求都不送）。
- 外部字串（標題、來源名、川普摘錄、預測市場名稱）存之前一律再過 `market_calc.clean_external`。

obs 鍵（market_publish 讀；payload 全是 JSON 值、日期一律 ISO 字串）見下方 `OBS_*` 常數。
這個模組 import 時只把任務登記進框架的記憶體表，不做任何 I/O；sources 到真的要抓時才載入。
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

import market_tasks

UTC = timezone.utc

# ── 存放位置（market_publish 讀這些名字；要改兩邊一起改） ─────────────────────────────────
OBS_MACRO_PREFIX = "macro."  # ＋DXY／WTI／BRENT／GOLD／SILVER／BTC／RUT／DJI／IXIC／MOVE／USDTWD：
#   {"value","chg"|None,"chg_pct"|None,"date","prev_date"|None,"source","symbol","bars": [[iso, 收盤], …]}
OBS_NEWS_PREFIX = "news."  # ＋us／tw／iran：{"items": [{"title","source","published_utc"}],"fetched_utc"}
OBS_TRUMP_SUMMARY = "trump.summary"  # {"window_h","topics": {代號: 則數},"labels","posts","matched","latest_utc","at_least"}
OBS_TRUMP_POSTS = "trump.posts"  # {"items": [{"id","published_utc","text","topics","quoted_author"}]}（細節頁）
OBS_ELECTION_PREFIX = "election."  # ＋PREDICTION_MARKETS 的 key（polymarket.house、kalshi.senate…）：PredictionQuote 欄位＋mid、fetched_utc
POSTS_TRUMP = "trump"  # store.upsert_posts 的 kind

SOURCE_FX = "代理：期交所匯率"  # Yahoo 的 TWD=X 拿不到時的代理（market_dashboard 看到「代理」就標「（代理）」）
OBS_TW_FX = "tw.fx"  # market_pipeline_tw.OBS_FX（不 import 那個模組：只載入本模組時不該順便登記台股任務）

# ── 任務名稱與排程 ───────────────────────────────────────────────────────────────────
TASK_MACRO = "macro.yahoo"
TASK_TRUMP = "trump.feed"
NEWS_TOPICS = ("us", "tw", "iran")
STATE_PREFIX = "misc.pipe:"
NOT_YET, PARTIAL = market_tasks.NOT_YET, market_tasks.PARTIAL

PROXY_LOOKBACK_DAYS = 45  # 代理日線抓多長：日變化只要前一筆，多抓一點給 publish 看走勢
# 2026-10-23：ETF 代理要等 SPY 有 D 的正式列才抓（見 _nasdaq_has_d），所以第一次就該抓得到；萬一那個代理還是沒有 D 那根，
# 每小時再抓一次、最多再 3 次（每個代理每個 D 最多 4 個請求）。原本是 30 分鐘、1 次，剛開打就放棄。
PROXY_RETRY_S = 3600.0
PROXY_RETRIES = 3
GATE_SYMBOL = "SPY"  # Nasdaq 日線端點公布 D 了沒，看 market_pipeline_us 一直在追的 SPY
BARS_KEEP = 25
RETRY_AFTER_FAIL_S = 1800.0  # 固定間隔的來源失敗後多久再試（帳本冷卻另外算）
COOLDOWN_S = 7200.0  # 429／403（Google News 另加 503）之後，和 market_http 的冷卻一樣長
NEWS_MAX_AGE_H = 36.0
NEWS_FUTURE_SKEW = timedelta(minutes=5)
NEWS_KEEP = 15
TRUMP_WINDOW_H = 48.0
TRUMP_FUTURE_SKEW_S = 300.0  # 和 market_sources_misc.FUTURE_SKEW（5 分鐘）一樣：比這更未來的 last_newest 不可能是真的
TRUMP_POSTS_KEEP = 20
TRUMP_GAPS_KEEP = 20
ELECTION_RESOLVED_EVERY_S = 86400.0
ELECTION_SHOW_RESULT_S = 7 * 86400.0
_DEFAULT_EVERY_S = {"gnews.us": 1800, "gnews.tw": 1800, "gnews.iran": 3600, "trumpstruth": 1800}
_RATE_LIMIT_KINDS = frozenset({"rate_limited", "blocked"})


# ── 共用 ──────────────────────────────────────────────────────────────────────────
def _misc() -> Any:
    import market_sources_misc  # noqa: PLC0415 - 真的要抓時才載入

    return market_sources_misc


def _fetch_error() -> type:
    import market_http  # noqa: PLC0415

    return market_http.FetchError


class _NoRequest(Exception):
    """帳本擋下：沒有送出請求（budget＝間隔或每日上限、這輪要停；cooldown＝冷卻中）。"""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def _kind(exc: BaseException) -> str:
    kind = getattr(exc, "kind", None)
    return kind if isinstance(kind, str) and kind else f"unexpected:{type(exc).__name__}"


def _is_parse_error(exc: BaseException) -> bool:
    return isinstance(exc, ValueError) and isinstance(getattr(exc, "kind", None), str)


def _is_source_error(exc: BaseException) -> bool:
    return isinstance(exc, _fetch_error()) or _is_parse_error(exc)


def _call(ctx: Any, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    fetch_error = _fetch_error()
    try:
        return fn(*args, http=ctx.http, ledger=ctx.ledger, **kwargs)
    except fetch_error as exc:
        if getattr(exc, "kind", None) in ("budget", "cooldown"):
            raise _NoRequest(exc.kind) from None
        raise


def clean(text: Any, max_len: int) -> Optional[str]:
    """PLAN §19.1 的共用清洗（market_calc.clean_external）；回 None＝整則丟掉。"""
    import market_calc  # noqa: PLC0415

    return market_calc.clean_external(text, max_len)


def _now_utc(ctx: Any) -> datetime:
    return datetime.fromtimestamp(float(ctx.now), UTC)


def _state(store: Any, name: str) -> dict:
    return dict(store.task_state(STATE_PREFIX + name) or {})


def _save(store: Any, name: str, state: dict) -> None:
    store.set_task_state(STATE_PREFIX + name, state)


def _every(endpoint: str) -> float:
    try:
        return float(_misc().ENDPOINTS[endpoint].every_s)
    except (ImportError, AttributeError, KeyError, TypeError, ValueError):
        return float(_DEFAULT_EVERY_S[endpoint])


def _finite_positive(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return out if math.isfinite(out) and out > 0 else None


def interval_due(state: dict, now: float, every: float) -> bool:
    """固定間隔的來源：冷卻中不排；成功後隔 every、失敗後隔 min(every, 30 分鐘) 再試。"""
    cooldown = state.get("cooldown_until")
    if isinstance(cooldown, (int, float)) and now < float(cooldown):
        return False
    last_ok, last_try = state.get("last_ok"), state.get("last_try")
    if isinstance(last_ok, (int, float)) and now - float(last_ok) < every:
        return False
    if isinstance(last_try, (int, float)) and now - float(last_try) < min(every, RETRY_AFTER_FAIL_S):
        return False
    return True


def _fetch_once(ctx: Any, name: str, obs_ids: tuple[str, ...], fn: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Any, Optional[str]]:
    """固定間隔來源共用：抓一次、記時間；成功回 (結果, None)、帳本擋下回 (None, 狀態)，來源錯誤記 obs 後往上丟。"""
    state = _state(ctx.store, name)
    try:
        result = _call(ctx, fn, *args, **kwargs)
    except _NoRequest as exc:
        if exc.kind == "cooldown":
            state["cooldown_until"] = float(ctx.now) + COOLDOWN_S
            _save(ctx.store, name, state)
            raise _fetch_error()("cooldown") from None
        return None, NOT_YET
    except Exception as exc:
        if not _is_source_error(exc):
            raise
        state["last_try"] = float(ctx.now)
        state["last_kind"] = _kind(exc)
        if _kind(exc) in _RATE_LIMIT_KINDS:  # 429／403／Google News 的 503：該主機已在帳本冷卻，這裡也先不排
            state["cooldown_until"] = float(ctx.now) + COOLDOWN_S
        _save(ctx.store, name, state)
        for obs_id in obs_ids:
            ctx.store.obs_checked(obs_id, error_kind=_kind(exc))
        raise
    state["last_try"] = state["last_ok"] = float(ctx.now)
    state["last_kind"] = "ok"
    state.pop("cooldown_until", None)
    _save(ctx.store, name, state)
    return result, None


# ── 宏觀（Yahoo spark → 失敗改代理） ───────────────────────────────────────────────────
def us_session_d(ctx: Any) -> Optional[date]:
    """D：美東收盤＋30 分鐘已過的最近一個交易日（market_calendar 判斷）。"""
    import market_calendar  # noqa: PLC0415

    try:
        return market_calendar.latest_closed_us_session(ctx.us_cal, ctx.now)
    except Exception:  # noqa: BLE001 - 日曆壞掉：這輪不抓宏觀
        return None


def _prev_session(cal: Any, day: date) -> Optional[date]:
    try:
        return cal.prev_session(day)
    except Exception:  # noqa: BLE001
        return None


def macro_payload(bars: Any, d: date, cal: Any, source: str, symbol: str) -> Optional[tuple[dict, date]]:
    """日線 [(日期, 收盤)] → 一格宏觀值；晚於 D 的列丟掉。日變化只在前一筆＝前一個交易日時才給（§18.8）。"""
    rows: dict[date, float] = {}
    for item in bars or []:
        try:
            day, close = item[0], _finite_positive(item[1])
        except (TypeError, IndexError):
            continue
        if isinstance(day, date) and not isinstance(day, datetime) and close is not None and day <= d:
            rows[day] = close
    if not rows:
        return None
    ordered = sorted(rows.items())
    day, value = ordered[-1]
    chg = chg_pct = None
    prev_date: Optional[date] = None
    if len(ordered) >= 2:
        before, before_value = ordered[-2]
        if _prev_session(cal, day) == before:
            prev_date = before
            chg = value - before_value
            chg_pct = chg / before_value * 100.0
    payload = {
        "value": value,
        "chg": chg,
        "chg_pct": chg_pct,
        "date": day.isoformat(),
        "prev_date": prev_date.isoformat() if prev_date else None,
        "source": source,
        "symbol": symbol,
        "bars": [[item_day.isoformat(), close] for item_day, close in ordered[-BARS_KEEP:]],
    }
    return payload, day


def _macro_obs(key: str) -> str:
    return OBS_MACRO_PREFIX + key


def _nasdaq_has_d(ctx: Any, d: date) -> bool:
    """Nasdaq 日線端點公布 D 那天的正式收盤了嗎？ETF 代理和 SPY 走同一個端點，D 那列要收盤後好幾個小時才出現
    （2026-10-23 實測：04:43 打、30 分鐘後放棄，8 列代理整天停在前一個交易日）。看 SPY 在 closes 表有沒有 D 的
    正式列——CBOE 的 provisional 不算，它不代表 Nasdaq 公布了。有了才去抓代理，不必盲目探測。"""
    try:
        return any(row[0] == d and not row[4] for row in ctx.store.closes("us", GATE_SYMBOL, since=d))
    except Exception:  # noqa: BLE001 - 讀不到就當還沒有：這輪不打，下一輪再看
        return False


def _tw_fx_pairs(ctx: Any) -> Optional[list[tuple[date, float]]]:
    """market_pipeline_tw 存的期交所匯率（tw.fx）→ [(日期, USD/NTD)]；沒有這筆 obs 回 None。"""
    obs = ctx.store.get_obs(OBS_TW_FX) or {}
    payload = obs.get("payload") if isinstance(obs.get("payload"), dict) else None
    if payload is None:
        return None
    pairs: list[tuple[date, float]] = []
    for day_key, value_key in (("prev_date", "prev_usd_ntd"), ("date", "usd_ntd")):
        day, value = payload.get(day_key), payload.get(value_key)
        if isinstance(day, str) and isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                pairs.append((date.fromisoformat(day), float(value)))
            except ValueError:
                continue
    return pairs


def _usdtwd_behind(ctx: Any, d: date) -> bool:
    """tw.fx 的資料日（≤D）比卡上 macro.USDTWD 那格新嗎？期交所的匯率檔台北早上才更新、market_pipeline_tw 隔一陣子
    才讀到，這時宏觀任務早就在 D 換日那一刻讀過舊的 tw.fx 了（2026-10-23：台幣停在前一天）。只重算期交所代理的那格
    （Yahoo 的 TWD=X 不看 tw.fx）；只讀 obs，不送請求。"""
    pairs = _tw_fx_pairs(ctx)
    candidate = macro_payload(pairs, d, ctx.tw_cal, SOURCE_FX, "USD/NTD") if pairs else None
    if candidate is None:
        return False
    current = (ctx.store.get_obs(_macro_obs("USDTWD")) or {}).get("payload")
    if not isinstance(current, dict):
        return True  # 卡上還沒有這格，tw.fx 有了就補上
    if current.get("source") != SOURCE_FX:
        return False
    try:
        current_day = date.fromisoformat(current.get("date"))
    except (TypeError, ValueError):
        return True
    return candidate[1] > current_day


def macro_plan(ctx: Any) -> Optional[dict]:
    """這輪宏觀要做什麼：新的 D → 先打 Yahoo；同一個 D 還有代理在等 → 只抓那幾個（ETF 代理等 Nasdaq 公布 D、
    沒有 D 那根的每小時再抓）；tw.fx 比卡上的台幣新 → 重算台幣。"""
    d = us_session_d(ctx)
    if d is None:
        return None
    state = _state(ctx.store, TASK_MACRO)
    if state.get("d") != d.isoformat():
        return {"d": d, "yahoo": True, "keys": []}
    misc = _misc()
    pending = {key: dict(info) for key, info in (state.get("pending") or {}).items() if isinstance(info, dict)}
    # 2026-10-23：BTC 以前不走代理（主格是 Coinbase），部署時這個 D 的紀錄裡 BTC 既沒有來源、也不在 pending：
    # 當成還沒試過，補抓一次（之後每個代理鍵一定是「有來源」或「在 pending」）
    sources = state.get("sources") or {}
    for key in misc.PROXY_ETF:
        if key not in sources and key not in pending:
            pending[key] = {"tries": 0}
    gate: Optional[bool] = None  # 要用到才查（SPY 有沒有 D 的正式列）
    ready = []
    for key, info in sorted(pending.items()):
        last = info.get("last")
        if int(info.get("tries", 0)) > PROXY_RETRIES or (
            isinstance(last, (int, float)) and ctx.now - float(last) < PROXY_RETRY_S
        ):
            continue
        if key in misc.PROXY_ETF:
            gate = _nasdaq_has_d(ctx, d) if gate is None else gate
            if not gate:
                continue
        ready.append(key)
    if "USDTWD" not in ready and _usdtwd_behind(ctx, d):
        ready.append("USDTWD")
    return {"d": d, "yahoo": False, "keys": sorted(ready)} if ready else None


def _put_macro(ctx: Any, key: str, result: Optional[tuple[dict, date]]) -> Optional[date]:
    if result is None:
        return None
    payload, day = result
    ctx.store.put_obs(_macro_obs(key), payload, data_date=day.isoformat())
    return day


def _macro_run(ctx: Any) -> Optional[str]:
    plan = macro_plan(ctx)
    if plan is None:
        return NOT_YET
    misc = _misc()
    d = plan["d"]
    state = _state(ctx.store, TASK_MACRO)
    keys: list[str] = list(plan["keys"])
    if plan["yahoo"]:
        series: Optional[dict] = None
        try:
            series = _call(ctx, misc.fetch_yahoo_spark, through=d)
            yahoo_kind = "ok"
        except _NoRequest as exc:
            if exc.kind == "budget":  # 這輪時間到或間隔不夠：沒打出去，下一輪再來
                return NOT_YET
            yahoo_kind = "cooldown"
        except Exception as exc:
            if not _is_source_error(exc):
                raise
            yahoo_kind = _kind(exc)
        state = {"d": d.isoformat(), "yahoo": yahoo_kind, "pending": {}, "sources": {}}
        keys = []
        for key, symbol in misc.YAHOO_MACRO.items():
            got = _put_macro(ctx, key, macro_payload((series or {}).get(symbol), d, ctx.us_cal, misc.SOURCE_YAHOO, symbol))
            if got is not None:
                state["sources"][key] = misc.SOURCE_YAHOO
            else:
                keys.append(key)
    status = _run_proxies(ctx, d, keys, state)
    _save(ctx.store, TASK_MACRO, state)
    return status


def _run_proxies(ctx: Any, d: date, keys: list[str], state: dict) -> Optional[str]:
    """Yahoo 拿不到的那幾格改代理；回這輪的狀態（全部到手 None、有缺 partial）。"""
    misc = _misc()
    pending: dict = state.setdefault("pending", {})
    sources: dict = state.setdefault("sources", {})
    yahoo_kind = str(state.get("yahoo") or "unavailable")
    missing = False
    gate: Optional[bool] = None  # Nasdaq 公布 D 了沒（只有 ETF 代理要看；台幣不走 Nasdaq）
    for index, key in enumerate(keys):
        if ctx.should_stop():  # 剩下的下一輪再來（不算一次）
            for rest in keys[index:]:
                pending.setdefault(rest, {"tries": 0})
            return PARTIAL
        obs_id = _macro_obs(key)
        record = pending.get(key) or {"tries": 0}
        if key == "MOVE":  # MOVE 沒有代理：只記 Yahoo 失敗、留著舊值（BTC 有 IBIT 代理，2026-10-23）
            ctx.store.obs_checked(obs_id, error_kind=_obs_kind(yahoo_kind))
            pending.pop(key, None)
            missing = True
            continue
        if key in misc.PROXY_ETF:
            gate = _nasdaq_has_d(ctx, d) if gate is None else gate
            if not gate:  # Nasdaq 還沒公布 D：先不打，等 SPY 有 D 的正式列（macro_plan 每輪看），這不算一次
                pending.setdefault(key, {"tries": 0})
                missing = True
                continue
        try:
            if key == "USDTWD":
                result = _usdtwd_proxy(ctx, d)
                label, etf = SOURCE_FX, "USD/NTD"
            else:
                etf = misc.PROXY_ETF[key]
                label = misc.proxy_source_label(etf)
                bars = _call(ctx, misc.fetch_proxy_bars, etf, d - timedelta(days=PROXY_LOOKBACK_DAYS), d)
                result = macro_payload(bars, d, ctx.us_cal, label, etf)
        except _NoRequest:  # 帳本擋下：沒有送出，下一輪再來
            pending[key] = {"tries": int(record.get("tries", 0)), "last": None}
            missing = True
            continue
        except Exception as exc:
            if not _is_source_error(exc):
                raise
            ctx.store.obs_checked(obs_id, error_kind=_kind(exc))
            pending[key] = {"tries": int(record.get("tries", 0)) + 1, "last": float(ctx.now)}
            missing = True
            continue
        got = _put_macro(ctx, key, result)
        if got is None:
            ctx.store.obs_checked(obs_id, error_kind="empty")
        else:
            sources[key] = label
        if got is None or got < d:  # 沒有 D 那根：每小時再抓一次（PROXY_RETRY_S、PROXY_RETRIES）
            pending[key] = {"tries": int(record.get("tries", 0)) + 1, "last": float(ctx.now)}
            missing = True
        else:
            pending.pop(key, None)
    return PARTIAL if missing else None


def _obs_kind(kind: str) -> str:
    return kind if kind and kind != "ok" else "yahoo_missing"


def _usdtwd_proxy(ctx: Any, d: date) -> Optional[tuple[dict, date]]:
    """台幣代理：先用 market_pipeline_tw 存的期交所匯率（tw.fx），沒有才自己抓一次。"""
    pairs = _tw_fx_pairs(ctx)
    if pairs is None:
        pairs = []
        import market_sources_tw  # noqa: PLC0415

        row = _call(ctx, market_sources_tw.fetch_taifex_fx)
        pairs.append((row.date, float(row.usd_ntd)))
        if row.prev_date is not None and row.prev_usd_ntd is not None:
            pairs.append((row.prev_date, float(row.prev_usd_ntd)))
    return macro_payload(pairs, d, ctx.tw_cal, SOURCE_FX, "USD/NTD")


def _macro_due(ctx: Any) -> bool:
    return macro_plan(ctx) is not None


# ── 新聞（Google News RSS） ─────────────────────────────────────────────────────────
def news_items(items: Any, now_utc: datetime) -> list[dict]:
    """解析後的新聞再驗一次：36 小時內、不在未來、標題與來源清洗得過；新到舊。"""
    cutoff = now_utc - timedelta(hours=NEWS_MAX_AGE_H)
    out: list[tuple[datetime, dict]] = []
    seen: set[tuple[str, str]] = set()
    for item in items or []:
        published = getattr(item, "published_utc", None)
        if not isinstance(published, datetime) or published.tzinfo is None:
            continue
        if published < cutoff or published > now_utc + NEWS_FUTURE_SKEW:
            continue
        title, source = clean(getattr(item, "title", None), 40), clean(getattr(item, "source", None), 24)
        if title is None or source is None or (title, source) in seen:
            continue
        seen.add((title, source))
        out.append((published, {"title": title, "source": source, "published_utc": published.astimezone(UTC).isoformat()}))
    out.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in out[:NEWS_KEEP]]


def _news_fns(topic: str) -> tuple[Callable[[Any], bool], Callable[[Any], Optional[str]]]:
    name = f"news.{topic}"
    obs_id = OBS_NEWS_PREFIX + topic

    def due(ctx: Any) -> bool:
        return interval_due(_state(ctx.store, name), float(ctx.now), _every(f"gnews.{topic}"))

    def run(ctx: Any) -> Optional[str]:
        now_utc = _now_utc(ctx)
        items, status = _fetch_once(ctx, name, (obs_id,), _misc().fetch_google_news, topic, now_utc)
        if status is not None:
            return status
        kept = news_items(items, now_utc)
        newest = kept[0]["published_utc"][:10] if kept else now_utc.date().isoformat()
        ctx.store.put_obs(obs_id, {"items": kept, "fetched_utc": now_utc.isoformat()}, data_date=newest)
        return None

    return due, run


# ── 川普原文（trumpstruth RSS；依 originalId 累積 48 小時） ─────────────────────────────────
def update_coverage(state: dict, times: list[float], now: float, *, reaches_back: bool = False) -> None:
    """這輪抓到的貼文時間 → 缺口紀錄。times＝整份 feed 在時間界線內的最舊與最新一則（market_sources_misc.FeedSpan）。

    上輪最新一則還在 48 小時視窗內：這輪最舊比它還新＝中間有缺口（reaches_back 不採信：一則置頂的舊 item
    不能把真的缺口蓋掉）。剛上線、或上輪最新一則已經滑出視窗：前後接不上，這一份 feed 要自己涵蓋整個視窗
    （reaches_back＝feed 還有比視窗起點更舊的 item）；涵蓋不到，最舊那則之前都不知道。（2026-10-23 審查 r7b F1）
    """
    if not times:
        return
    oldest, newest = min(times), max(times)
    horizon = now - TRUMP_WINDOW_H * 3600.0
    gaps = [list(gap) for gap in state.get("gaps") or []]
    last_newest = state.get("last_newest")
    if isinstance(last_newest, (int, float)) and last_newest <= now + TRUMP_FUTURE_SKEW_S:
        last_newest = float(last_newest)
    else:
        last_newest = None  # 沒有；或是未來的時間（舊版沒擋時間打錯的 item，max() 又不會讓它退回來）：當作還沒成功過
    if last_newest is not None and last_newest >= horizon:
        if oldest > last_newest:
            gaps.append([last_newest, oldest])
    elif not reaches_back:
        gaps.append([last_newest, oldest])
    state["last_newest"] = max(newest, last_newest) if last_newest is not None else newest
    state["gaps"] = [gap for gap in gaps if gap[1] > horizon][-TRUMP_GAPS_KEEP:]


def at_least(state: dict, now: float) -> bool:
    """近 48 小時的計數是不是只能說「至少」：從沒成功過、或還有缺口落在 48 小時內。"""
    if not isinstance(state.get("last_newest"), (int, float)):
        return True
    horizon = now - TRUMP_WINDOW_H * 3600.0
    return any(isinstance(gap[1], (int, float)) and gap[1] > horizon for gap in state.get("gaps") or [])


def _trump_item(post: Any, labels: dict) -> Optional[dict]:
    original_id = getattr(post, "original_id", None)
    published = getattr(post, "published_utc", None)
    if not isinstance(original_id, str) or not original_id.isascii() or not original_id.isdigit() or len(original_id) > 25:
        return None
    if not isinstance(published, datetime) or published.tzinfo is None:
        return None
    removed = bool(getattr(post, "removed", False))
    own = getattr(post, "own_text", None)
    quoted = getattr(post, "quoted_author", None)
    return {
        "original_id": original_id,
        "published_utc": published.astimezone(UTC),
        "own_text": None if removed or not own else clean(own, 100),
        "quoted_author": None if removed or not quoted else clean(quoted, 24),
        "post_type": clean(getattr(post, "post_type", "") or "", 20) or "",
        "removed": removed,
        # 主題是程式自己的代號（片語表比對完整的本人文字）；摘錄被清洗丟掉時主題照算
        "topics": [] if removed else [topic for topic in (getattr(post, "topics", ()) or ()) if topic in labels],
    }


def trump_summary(store: Any, now_utc: datetime, state: dict, labels: dict) -> tuple[dict, dict]:
    """從累積的貼文算近 48 小時：主題則數（摘要卡）與最近的摘錄（細節頁）。"""
    since = now_utc - timedelta(hours=TRUMP_WINDOW_H)
    posts = [post for post in store.posts(POSTS_TRUMP, since_utc=since) if not post.get("removed")]
    counts: Counter = Counter()
    matched = 0
    details: list[dict] = []
    for post in posts:
        topics = [topic for topic in dict.fromkeys(post.get("topics") or []) if topic in labels]
        if topics:
            matched += 1
            counts.update(topics)
            if post.get("own_text") and len(details) < TRUMP_POSTS_KEEP:
                details.append(
                    {
                        "id": post.get("id"),
                        "published_utc": post["published_utc"].isoformat(),
                        "text": post.get("own_text"),
                        "topics": topics,
                        "quoted_author": post.get("quoted_author"),
                    }
                )
    latest = max((post["published_utc"] for post in posts), default=None)
    summary = {
        "window_h": TRUMP_WINDOW_H,
        "topics": dict(sorted(counts.items())),
        "labels": {topic: labels[topic] for topic in sorted(counts)},
        "posts": len(posts),
        "matched": matched,
        "latest_utc": latest.isoformat() if latest else None,
        "at_least": at_least(state, now_utc.timestamp()),
    }
    return summary, {"items": details}


def _trump_due(ctx: Any) -> bool:
    return interval_due(_state(ctx.store, TASK_TRUMP), float(ctx.now), _every("trumpstruth"))


def _trump_run(ctx: Any) -> Optional[str]:
    misc = _misc()
    now_utc = _now_utc(ctx)
    result, status = _fetch_once(
        ctx, TASK_TRUMP, (OBS_TRUMP_SUMMARY,), misc.fetch_trumpstruth_with_span, now_utc, include_removed=True
    )
    if status is not None:
        return status
    posts, span = result
    labels = dict(misc.TRUMP_TOPIC_LABELS)
    items = [item for item in (_trump_item(post, labels) for post in posts or []) if item is not None]
    if items:
        ctx.store.upsert_posts(POSTS_TRUMP, items)
    state = _state(ctx.store, TASK_TRUMP)
    # 缺口用整份 feed（過濾前）的頭尾判斷：只有圖／影片的貼文也在 feed 裡，用過濾後的 items 會誤判成有漏、
    # 多標「（至少）」（2026-10-18 審查 r6c N1）；頭尾套了和 parse 一樣的時間界線（未來的不算、太舊的只記
    # reaches_back），一則時間打錯或置頂的 item 不能讓真的缺口標不出來（2026-10-23 審查 r7b F1）
    update_coverage(state, [span.oldest.timestamp(), span.newest.timestamp()], float(ctx.now), reaches_back=span.reaches_back)
    _save(ctx.store, TASK_TRUMP, state)
    summary, details = trump_summary(ctx.store, now_utc, state, labels)
    day = now_utc.date().isoformat()
    ctx.store.put_obs(OBS_TRUMP_SUMMARY, summary, data_date=day)
    ctx.store.put_obs(OBS_TRUMP_POSTS, details, data_date=day)
    return None


# ── 期中選舉預測市場（Polymarket、Kalshi；固定市場，每 2 小時） ─────────────────────────────
def election_due(state: dict, now: float, every: float) -> bool:
    """結果出來前每 2 小時；出來後每天一次、顯示 7 天後不再抓。"""
    resolved = state.get("resolved_at")
    if isinstance(resolved, (int, float)):
        if now - float(resolved) >= ELECTION_SHOW_RESULT_S:
            return False
        every = max(every, ELECTION_RESOLVED_EVERY_S)
    return interval_due(state, now, every)


def _prediction_blocked() -> bool:
    """market_sources_misc.PREDICTION_MARKETS_BLOCKED（2026-10-23：台灣封鎖 Polymarket、Kalshi）。
    只有明確設成 False 才恢復抓取；設成別的值照樣封鎖，一個請求都不送。"""
    return getattr(_misc(), "PREDICTION_MARKETS_BLOCKED", True) is not False


def _retire_failures(ctx: Any, name: str) -> None:
    """封鎖中的任務不再執行，框架留在 task_state 的連續失敗次數不會自己歸零，每日維護會天天把它當失敗報：
    第一次看到封鎖時把失敗次數與退避時間清掉一次（只在有東西可清時才寫；清不掉不能讓這輪 collect 出錯）。"""
    try:
        state = dict(ctx.store.task_state(name) or {})
        if state.get("fail_streak") or state.get("next_try") is not None:
            state.update(fail_streak=0, next_try=None)
            ctx.store.set_task_state(name, state)
    except Exception:  # noqa: BLE001
        return


def _election_fns(market: Any) -> tuple[str, Callable[[Any], bool], Callable[[Any], Optional[str]]]:
    name = f"election.{market.key}"
    obs_id = OBS_ELECTION_PREFIX + market.key

    def due(ctx: Any) -> bool:
        if _prediction_blocked():  # 封鎖中：任務留在清單裡，但永遠不到期（不送請求；主機也不在白名單）
            _retire_failures(ctx, name)
            return False
        return election_due(_state(ctx.store, name), float(ctx.now), _every_or(market.key, 7200))

    def run(ctx: Any) -> Optional[str]:
        quote, status = _fetch_once(ctx, name, (obs_id,), _misc().fetch_prediction_quote, market)
        if status is not None:
            return status
        now_utc = _now_utc(ctx)
        updated = getattr(quote, "updated_utc", None)
        payload = {
            "platform": clean(quote.platform, 16),
            "market_id": clean(quote.market_id, 40),
            "outcome": clean(quote.outcome, 24),
            "bid": quote.bid,
            "ask": quote.ask,
            "mid": quote.mid,
            "last": quote.last,
            "updated_utc": updated.astimezone(UTC).isoformat() if isinstance(updated, datetime) else None,
            "active": bool(quote.active),
            "closed": bool(quote.closed),
            "result": clean(quote.result, 8) if quote.result else None,
            "fetched_utc": now_utc.isoformat(),
        }
        ctx.store.put_obs(obs_id, payload, data_date=now_utc.date().isoformat())
        if quote.closed and quote.result:
            state = _state(ctx.store, name)
            state.setdefault("resolved_at", float(ctx.now))
            _save(ctx.store, name, state)
        return None

    return name, due, run


def _every_or(endpoint: str, default: float) -> float:
    try:
        return float(_misc().ENDPOINTS[endpoint].every_s)
    except (ImportError, AttributeError, KeyError, TypeError, ValueError):
        return float(default)


# ── 任務清單與註冊 ───────────────────────────────────────────────────────────────────
def tasks() -> list[market_tasks.Task]:
    out: list[market_tasks.Task] = []
    for topic in NEWS_TOPICS:
        due, run = _news_fns(topic)
        out.append(market_tasks.Task(f"news.{topic}", "fast", due, run, 8.0))
    out.append(market_tasks.Task(TASK_TRUMP, "fast", _trump_due, _trump_run, 10.0))
    for market in _prediction_markets():
        name, due, run = _election_fns(market)
        out.append(market_tasks.Task(name, "medium", due, run, 10.0))
    out.append(market_tasks.Task(TASK_MACRO, "core", _macro_due, _macro_run, 60.0))
    return out


def _prediction_markets() -> tuple:
    """固定市場清單在 market_sources_misc（它核對 outcome）；import 不做 I/O，只讀常數。"""
    return tuple(_misc().PREDICTION_MARKETS)


def register_all() -> None:
    for task in tasks():
        market_tasks.register(task)


register_all()
