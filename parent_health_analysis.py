"""爸媽健康分析（/健康 分析，2026-10-17）：用固定門檻與固定句子，從已存的健康紀錄整理「可以注意」
和「做得好」的地方。分析卡、週日私訊 Andrew、每日檢查共用這裡的規則。

Andrew 2026-10-17：「爸媽健康加入分析機制，根據抓到的資訊分析有無要改善的地方」，看完提案選全 A
（規則式、不送模型；群組分析卡＋週日私訊完整版；卡片不寫 119，當下怎麼做只在私訊）。

- 純函式、只用標準庫：不 import parent_health／parent_health_db／parent_health_extract，不讀寫 DB、
  檔案或網路，import 時什麼都不做。parent_health 在函式裡才 import 這個模組（這裡壞了不影響 119）。
- 輸入是 parent_health._events() 的列（渲染前已驗過固定詞表），呼叫端已濾掉 on_since 以前與
  現在以後的列。輸出只有固定樣板、數字、日期、詞表標籤與看診詞；沒有留言原文、沒有名字。
- 只根據「有提到」的事：沒提到不等於沒做，不從沒提到推論。
- 天數一律是不同天數；同一天同一個讀數只算一次（家長和家人各說一次不重算）。

門檻與出處（2026-10-17 查證）：
- 血壓平均 ≥130/80、722 量法：2022 台灣高血壓指引（國健署）。
- 單次 >180/120：美國心臟協會血壓分級（高血壓危象）。收縮壓 <90：美國 NHLBI 低血壓（低於 90/60）。
- 血糖 <70：2022 第 2 型糖尿病臨床照護指引第一級低血糖；≥200：臺大醫院「任一時間血糖 ≥ 200」。
- 心跳 >100 偏快、<50 偏慢；體重 6 個月內非刻意下降 ≥5% 要查原因；長者運動 WHO 2020（加肌力、平衡）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

__all__ = [
    "Finding",
    "Heard",
    "analyze",
    "card_lines",
    "last_heard",
    "readings_summary",
    "urgent_readings",
    "urgent_dm_text",
    "urgent_group_text",
    "TONE_EMOJI",
    "STRENGTH_BALANCE",
    "OTHER_EXERCISE",
    "GENERIC_EXERCISE",
    "RED_FLAG_LABELS",
]

_TPE = ZoneInfo("Asia/Taipei")
_WEEKDAYS = "一二三四五六日"

ALERT, NOTE, GOOD = "alert", "note", "good"
TONE_EMOJI = {ALERT: "⚠️", NOTE: "📝", GOOD: "👍"}

# 和 parent_health_extract.RED_FLAG_LABELS 相同（測試另外載真的詞表比對）；這裡不 import 抽取模組。
RED_FLAG_LABELS: tuple[str, ...] = (
    "胸痛胸悶", "呼吸困難", "嘴歪或說話不清", "單側無力", "中風", "心臟病發", "昏倒", "跌倒",
)
_REDFLAG = "redflag"
_BP, _GLUCOSE, _WEIGHT, _HEART = "血壓", "血糖", "體重", "心跳"
_READING_LABELS = frozenset({_BP, _GLUCOSE, _WEIGHT, _HEART})

# 運動分類：詞表裡每個運動標籤都要在其中一類（測試確認；部署 #2 會新增「氣功」）。
STRENGTH_BALANCE = frozenset({"太極", "外丹功", "氣功", "瑜伽", "重訓", "健身", "做操"})
OTHER_EXERCISE = frozenset({"散步", "爬山", "游泳", "騎腳踏車", "跳舞", "甩手", "拉筋", "跑步", "打球", "元極舞"})
GENERIC_EXERCISE = frozenset({"運動"})  # 不知道做了什麼：不給「加肌力、平衡」的建議

_MED_TAKEN, _MED_FORGOT, _MED_STOP = "吃藥", "忘了吃藥", "停藥"
_SLEEP_BAD = "睡不好"
_MEAL_SKIP = frozenset({"沒吃早餐", "沒吃午餐", "沒吃晚餐", "沒吃飯"})
_APPETITE, _ALCOHOL, _QUIT_ALCOHOL = "胃口不好", "喝酒", "戒酒"
_GOOD_DIET = ("少吃鹽", "少吃油", "少吃甜", "吃清淡", "控制飲食", "戒糖", _QUIT_ALCOHOL)
_INTENTIONAL_WEIGHT = frozenset({"控制飲食", "戒糖", "少吃甜", "少吃油"})  # 有在控制飲食：體重下降不當成警訊
_DIZZY = "頭暈"
# 症狀標籤和讀數規則講同一件事：讀數規則成立時不另列症狀
_SYMPTOM_COVERED_BY = {"血壓高": ("bp_crisis", "bp_high_avg"), "血壓低": ("bp_low",), "血糖高": ("glucose_high",)}
# 醫療指標只接這三種看診（同義詞在 parent_health._VISIT_CANONICAL 已經合併）；看牙、復健不接
_DOCTOR_VISITS = {"回診": "回診", "看醫生": "看診", "看診": "看診"}
_VISIT_HORIZON = 14

# 讀數合理範圍（和抽取時一樣；VALUE_RE 只管形狀）
_SYS_RANGE, _DIA_RANGE = (70, 250), (40, 150)
_GLUCOSE_RANGE, _HEART_RANGE, _WEIGHT_RANGE = (40, 500), (30, 200), (30, 150)

_BP_RE = re.compile(r"([0-9]{2,3})/([0-9]{2,3})")
_NUM_RE = re.compile(r"([0-9]{2,3})(?:\.([0-9]))?")

_DOCTOR_DEFAULT = "回診時跟醫生說"
_SLEEP_ADVICE = "固定起床時間、午睡 30 分鐘內、下午少喝咖啡和茶；持續的話跟醫生聊"
_STRENGTH_ADVICE = "再加點肌力、平衡運動更能防跌"


@dataclass(frozen=True)
class Finding:
    """一條分析：card＝卡片那行（不含 emoji），advice＝「→」後面那句（可空），dm＝週私訊那行。"""

    kind: str
    tone: str
    rank: int
    card: str
    advice: str
    dm: str
    order: tuple = ()  # 同 rank 時的次序（症狀天數多的在前）


@dataclass(frozen=True)
class Heard:
    """分析卡「最近聽到的」一類（2026-10-23）：最近一次聽到是哪天、哪個標籤（讀數另帶數字）、距今天幾天、
    近 14 天（D-13..D）有幾天提到。"""

    day: date
    item: str
    value: str
    days_ago: int
    days_14: int


# ── 小工具 ──────────────────────────────────────────────────────────────────


def _md(day: date) -> str:
    return f"{day.month}/{day.day}"


def _md_wd(day: date) -> str:
    return f"{day.month}/{day.day}（{_WEEKDAYS[day.weekday()]}）"


def _half_up(total: int, count: int) -> int:
    """total／count 四捨五入成整數（Python 的 round 是銀行家捨入：round(130.5)＝130）。"""
    return (2 * total + count) // (2 * count)


def _row_day(row) -> date | None:
    day = row.get("day") if isinstance(row, dict) else None
    return day if isinstance(day, date) else None


def _row_ts(row) -> float:
    try:
        return float(row.get("event_ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _in(rows, start: date, end: date) -> list[dict]:
    return [row for row in rows if (day := _row_day(row)) is not None and start <= day <= end]


def _days(rows, category: str, items) -> set[date]:
    items = {items} if isinstance(items, str) else set(items)
    return {
        _row_day(row)
        for row in rows
        if row.get("category") == category and row.get("item") in items and not row.get("flag")
    }


def _event_id(row) -> int:
    value = row.get("event_id")
    return value if isinstance(value, int) else 0


def _latest(rows):
    return max(rows, key=lambda row: (_row_ts(row), _event_id(row))) if rows else None


def _parse_bp(value) -> tuple[int, int] | None:
    match = _BP_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    systolic, diastolic = int(match.group(1)), int(match.group(2))
    if not (_SYS_RANGE[0] <= systolic <= _SYS_RANGE[1] and _DIA_RANGE[0] <= diastolic <= _DIA_RANGE[1]):
        return None
    if systolic <= diastolic:  # 「80/130」不是血壓
        return None
    return systolic, diastolic


def _parse_tenths(value, bounds: tuple[int, int]) -> int | None:
    """「62.5」→ 625（十分之一為單位的整數）；範圍外或格式不合回 None。"""
    match = _NUM_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    tenths = int(match.group(1)) * 10 + int(match.group(2) or 0)
    if not bounds[0] * 10 <= tenths <= bounds[1] * 10:
        return None
    return tenths


def _readings(rows, item: str, *, earliest: bool = False) -> list[dict]:
    """某種讀數的列：同一天同一個數字只留一筆（預設留最新那則；earliest＝留最早那則），舊到新。"""
    best: dict[tuple, dict] = {}
    for row in rows:
        if row.get("category") != "body" or row.get("item") != item or row.get("flag"):
            continue
        value = row.get("value")
        if not isinstance(value, str) or not value:
            continue
        if item == _BP:
            parsed = _parse_bp(value)
        else:
            bounds = {_GLUCOSE: _GLUCOSE_RANGE, _HEART: _HEART_RANGE, _WEIGHT: _WEIGHT_RANGE}[item]
            parsed = _parse_tenths(value, bounds)
        if parsed is None:
            continue
        key = (_row_day(row), value)
        if key[0] is None:
            continue
        current = best.get(key)
        if current is None:
            keep = True
        elif earliest:
            keep = _row_ts(row) < _row_ts(current["row"])
        else:
            keep = _row_ts(row) >= _row_ts(current["row"])
        if keep:
            best[key] = {"row": row, "value": value, "parsed": parsed, "day": key[0], "ts": _row_ts(row)}
    return sorted(best.values(), key=lambda r: (r["ts"], r["day"]))


def _doctor_visit(visits, today: date):
    """14 天內（含今天、含第 14 天）第一個回診／看醫生／看診 → (日期, 名稱)；沒有回 None。"""
    candidates = []
    for visit in visits or ():
        try:
            day, keyword = visit
        except (TypeError, ValueError):
            continue
        if isinstance(day, date) and keyword in _DOCTOR_VISITS and today <= day <= today + timedelta(days=_VISIT_HORIZON):
            candidates.append((day, _DOCTOR_VISITS[keyword]))
    return min(candidates) if candidates else None


def _doctor_advice(visit, *, extra: str = "") -> str:
    base = f"{_md_wd(visit[0])}{visit[1]}時跟醫生說" if visit else _DOCTOR_DEFAULT
    return base + extra


def _alert_advice(pronoun: str) -> str:
    """緊急的那幾條（緊急字詞、危象血壓、低血糖）：和總覽卡 💡 一樣先問現在的狀況；不寫 119、不寫等回診。"""
    return f"可以問問{pronoun}現在的狀況，也跟醫生說"


def _prev_text(count: int | None) -> str:
    return "" if count is None else f"（上週 {count} 天）"


# ── 規則 ────────────────────────────────────────────────────────────────────


def analyze(
    rows, *, today: date, visits=(), week: bool = False, prev_week_ok: bool = False, pronoun: str = ""
) -> list[Finding]:
    """一位家長的分析，已排序（alert → note → good）。

    rows：parent_health._events() 的列（呼叫端已濾掉 on_since 以前、現在以後的）；體重要給到 180 天前。
    visits：這位家長未來的看診 (日期, 看診詞)。
    week：週私訊用；單次事件（緊急字詞、單次讀數、停藥）只看這 7 天，不看 14 天（不會連兩週都列）。
    prev_week_ok：上週（D-13..D-7）整週都在記錄中，天數類加「（上週 N 天）」。
    pronoun：他／她（緊急字詞那條寫「可以問問他現在的狀況」）。"""
    rows = [row for row in (rows or ()) if isinstance(row, dict) and _row_day(row) is not None]
    w14 = _in(rows, today - timedelta(days=13), today)
    w7 = _in(rows, today - timedelta(days=6), today)
    prev = _in(rows, today - timedelta(days=13), today - timedelta(days=7)) if prev_week_ok else None
    single = w7 if week else w14
    visit = _doctor_visit(visits, today)
    found: list[Finding] = []
    fired: set[str] = set()

    def add(kind, tone, rank, card, advice, dm, order=()):
        found.append(Finding(kind, tone, rank, card, advice, dm, order))
        fired.add(kind)

    def prev_days(category, items):
        return None if prev is None else len(_days(prev, category, items))

    # 緊急字詞（只有當時判定緊急、flag=redflag 的；很久以前、已經好了的存成 flag='' 不算）
    redflags = [
        row for row in single
        if row.get("category") == "body" and row.get("flag") == _REDFLAG and row.get("item") in RED_FLAG_LABELS
    ]
    by_label: dict[str, dict] = {}
    for row in redflags:
        current = by_label.get(row["item"])
        if current is None or _row_ts(row) >= _row_ts(current):
            by_label[row["item"]] = row
    for label, row in sorted(by_label.items(), key=lambda pair: -_row_ts(pair[1])):
        day = _row_day(row)
        add("redflag", ALERT, -1, f"{_md(day)} 提到{label}", _alert_advice(pronoun),
            f"⚠️ {_md(day)} 提到{label}", (-_row_ts(row), label))

    bp_single = _readings(single, _BP)
    crisis = [r for r in bp_single if r["parsed"][0] > 180 or r["parsed"][1] > 120]
    if crisis:
        r = crisis[-1]
        systolic, diastolic = r["parsed"]
        parts = []
        if systolic > 180:
            parts.append("高壓超過 180")
        if diastolic > 120:
            parts.append("低壓超過 120")
        later = [x for x in bp_single if x["ts"] > r["ts"]]
        after = f"（之後 {_md(later[-1]['day'])} 量 {later[-1]['value']}）" if later else ""
        add("bp_crisis", ALERT, 0, f"{_md(r['day'])} 量到血壓 {r['value']}，{'、'.join(parts)}{after}",
            _alert_advice(pronoun), f"⚠️ {_md(r['day'])} 血壓 {r['value']}，{'、'.join(parts)}{after}")

    glucose_single = _readings(single, _GLUCOSE)
    low = [r for r in glucose_single if r["parsed"] < 700]
    if low:
        r = low[-1]
        add("glucose_low", ALERT, 1, f"{_md(r['day'])} 量到血糖 {r['value']}，低於 70",
            _alert_advice(pronoun), f"⚠️ {_md(r['day'])} 血糖 {r['value']}，低於 70")

    bp14 = _readings(w14, _BP)
    if len(bp14) >= 3 and len({r["day"] for r in bp14}) >= 2:
        systolic = _half_up(sum(r["parsed"][0] for r in bp14), len(bp14))
        diastolic = _half_up(sum(r["parsed"][1] for r in bp14), len(bp14))
        if systolic >= 130 or diastolic >= 80:
            advice = (f"{_md_wd(visit[0])}{visit[1]}" if visit else "回診") + "帶紀錄問醫生；可以照 722 量（連 7 天，起床後和睡前各量 2 遍）"
            add("bp_high_avg", NOTE, 10,
                f"血壓量了 {len(bp14)} 次，平均 {systolic}/{diastolic}，一般建議低於 130/80", advice,
                f"近 14 天血壓 {len(bp14)} 次平均 {systolic}/{diastolic}（一般建議低於 130/80）")

    high = [r for r in glucose_single if r["parsed"] >= 2000]
    if high:
        r = high[-1]
        add("glucose_high", NOTE, 11, f"{_md(r['day'])} 血糖 {r['value']}，在 200 以上", _doctor_advice(visit),
            f"{_md(r['day'])} 血糖 {r['value']}，在 200 以上")

    if "bp_high_avg" not in fired:
        lows = [r for r in bp14 if r["parsed"][0] < 90]
        dizzy = bool(_days(w14, "body", _DIZZY))
        if len(lows) >= 2 or (lows and dizzy):
            tail = "，也提到頭暈" if dizzy else ""
            add("bp_low", NOTE, 12, f"血壓有 {len(lows)} 次高壓低於 90{tail}",
                _doctor_advice(visit, extra="；起身動作慢一點"), f"近 14 天血壓有 {len(lows)} 次高壓低於 90{tail}")

    heart = _readings(w14, _HEART)
    fast = sum(1 for r in heart if r["parsed"] > 1000)
    slow = sum(1 for r in heart if r["parsed"] < 500)
    parts = ([f"{fast} 次超過 100"] if fast >= 2 else []) + ([f"{slow} 次低於 50"] if slow >= 2 else [])
    if parts:
        add("hr", NOTE, 13, f"心跳有 {'、'.join(parts)}", _doctor_advice(visit), f"近 14 天心跳有 {'、'.join(parts)}")

    weights = _readings(_in(rows, today - timedelta(days=179), today), _WEIGHT)
    intentional = bool(_days(_in(rows, today - timedelta(days=179), today), "diet", _INTENTIONAL_WEIGHT))
    if len(weights) >= 2 and not intentional:
        first, last = weights[0], weights[-1]
        f10, l10 = first["parsed"], last["parsed"]
        if (
            last["day"] >= today - timedelta(days=13)
            and (last["day"] - first["day"]).days >= 14
            and f10 > l10
            and (f10 - l10) * 20 >= f10
        ):
            percent = _half_up((f10 - l10) * 100, f10)
            text = f"體重從 {first['value']}（{_md(first['day'])}）到 {last['value']}（{_md(last['day'])}），少了 {percent}%"
            add("weight_loss", NOTE, 14, text,
                "留意食慾；" + (f"{_md_wd(visit[0])}{visit[1]}時跟醫生提" if visit else "回診時跟醫生提"), text)

    stops = [row for row in single if row.get("category") == "med" and row.get("item") == _MED_STOP and not row.get("flag")]
    if stops:
        day = _row_day(_latest(stops))
        add("med_stop", NOTE, 15, f"{_md(day)} 提到停藥", "要確認是醫生說可以停的", f"{_md(day)} 提到停藥")

    symptom_rows = [
        row for row in w7
        if row.get("category") == "body"
        and not row.get("value")
        and not row.get("flag")
        and row.get("item") not in RED_FLAG_LABELS
        and row.get("item") not in _READING_LABELS
    ]
    labels = sorted({row["item"] for row in symptom_rows})
    for label in labels:
        if any(kind in fired for kind in _SYMPTOM_COVERED_BY.get(label, ())):
            continue
        rows_for = [row for row in symptom_rows if row["item"] == label]
        days = len({_row_day(row) for row in rows_for})
        if days < 3:
            continue
        advice = f"{_md_wd(visit[0])}{visit[1]}時提" if visit else "可以安排看醫生"
        text = f"7 天有 {days} 天提到{label}"
        add(f"symptom:{label}", NOTE, 20, text, advice, text + _prev_text(prev_days("body", label)),
            (-days, -max(_row_ts(row) for row in rows_for), label))

    forgot = len(_days(w7, "med", _MED_FORGOT))
    if forgot >= 2:
        text = f"這週說忘了吃藥 {forgot} 天"
        add("med_forgot", NOTE, 30, text, "可以試分格藥盒或手機鬧鐘", text + _prev_text(prev_days("med", _MED_FORGOT)))

    bad_sleep = len(_days(w7, "sleep", _SLEEP_BAD))
    if bad_sleep >= 4:
        text = f"7 天有 {bad_sleep} 天睡不好"
        add("sleep_bad", NOTE, 31, text, _SLEEP_ADVICE, text + _prev_text(prev_days("sleep", _SLEEP_BAD)))

    skipped = len(_days(w7, "diet", _MEAL_SKIP))
    if skipped >= 2:
        text = f"7 天有 {skipped} 天沒吃正餐"
        add("diet_skip", NOTE, 32, text, "三餐盡量定時", text + _prev_text(prev_days("diet", _MEAL_SKIP)))

    appetite = len(_days(w7, "diet", _APPETITE))
    if appetite >= 2:
        text = f"7 天有 {appetite} 天胃口不好"
        add("appetite", NOTE, 33, text, "持續的話跟醫生提", text + _prev_text(prev_days("diet", _APPETITE)))

    alcohol = len(_days(w7, "diet", _ALCOHOL))
    if alcohol >= 3:
        text = f"7 天有 {alcohol} 天提到喝酒"
        add("alcohol", NOTE, 34, text, "少喝一點", text + _prev_text(prev_days("diet", _ALCOHOL)))

    # 做得好的
    exercise7 = [row for row in w7 if row.get("category") == "exercise" and not row.get("flag")]
    exercise_days = len({_row_day(row) for row in exercise7})
    if exercise_days >= 3:
        per_label: dict[str, set] = {}
        for row in exercise7:
            per_label.setdefault(row["item"], set()).add(_row_day(row))
        top = sorted(per_label, key=lambda item: (-len(per_label[item]), item))[:2]
        exercise14 = {row.get("item") for row in w14 if row.get("category") == "exercise" and not row.get("flag")}
        unknown = {item for item in exercise14 if item not in STRENGTH_BALANCE and item not in OTHER_EXERCISE}
        advice = "" if exercise14 & STRENGTH_BALANCE or unknown else _STRENGTH_ADVICE
        named = "" if set(top) <= GENERIC_EXERCISE else f"（{'、'.join(top)}）"
        text = f"這週 {exercise_days} 天有運動{named}，很好"
        add("good_exercise", GOOD, 90, text, advice,
            f"👍 這週 {exercise_days} 天有運動{named}" + _prev_text(
                None if prev is None else len({_row_day(row) for row in prev if row.get("category") == "exercise" and not row.get("flag")})
            ))

    taken = len(_days(w7, "med", _MED_TAKEN))
    if taken >= 5 and not forgot and "med_stop" not in fired:
        add("good_med", GOOD, 91, f"這週 {taken} 天說有吃藥，很規律", "", f"👍 這週 {taken} 天說有吃藥")

    good_diet = [
        row for row in w14
        if row.get("category") == "diet" and row.get("item") in _GOOD_DIET and not row.get("flag")
        and not (row.get("item") == _QUIT_ALCOHOL and "alcohol" in fired)
    ]
    if good_diet:
        label = _latest(good_diet)["item"]
        add("good_diet", GOOD, 92, f"有在{label}", "", f"👍 有在{label}")

    return sorted(found, key=lambda f: (f.rank, f.order))


def _valid_reading(item, value) -> bool:
    if item == _BP:
        return _parse_bp(value) is not None
    bounds = {_GLUCOSE: _GLUCOSE_RANGE, _HEART: _HEART_RANGE, _WEIGHT: _WEIGHT_RANGE}.get(item)
    return bounds is not None and _parse_tenths(value, bounds) is not None


def last_heard(rows, category: str, today: date, *, items=None) -> Heard | None:
    """最近一次聽到某一類是哪天（2026-10-23，分析卡「最近聽到的」、總覽卡運動列的「上次」）。

    rows：parent_health._analysis_rows（180 天內、這次開始記錄以來；呼叫端決定範圍，不是 _summary 的 7 天）。
    items：只算這些標籤（吃藥只算「吃藥」，不算忘了吃藥、停藥）；None＝這一類全部。
    紅旗列（flag 不空）一律不算；讀數標籤（血壓…）只算有合格讀數的列（症狀、緊急字詞不會被當成量了血壓）。
    今天以後的列不算。沒有回 None。只說「上次聽到」：沒聽到不等於沒做，不從這裡推論沒做。"""
    wanted = None if items is None else frozenset(items)
    picked = []
    for row in rows or ():
        if not isinstance(row, dict) or row.get("category") != category or row.get("flag"):
            continue
        item = row.get("item")
        if wanted is not None and item not in wanted:
            continue
        day = _row_day(row)
        if day is None or day > today:
            continue
        if item in _READING_LABELS and not _valid_reading(item, row.get("value")):
            continue
        picked.append(row)
    latest = _latest(picked)
    if latest is None:
        return None
    day = _row_day(latest)
    start = today - timedelta(days=13)
    days_14 = len({_row_day(row) for row in picked if _row_day(row) >= start})
    value = latest.get("value") if isinstance(latest.get("value"), str) else ""
    return Heard(day, str(latest.get("item") or ""), value, (today - day).days, days_14)


def card_lines(findings, cap: int = 3) -> tuple[list[Finding], int]:
    """卡片要顯示的條：alert 全部顯示；note 補到 cap；還有空位才放 1 條 good（good 不擠掉 note）。
    回 (要顯示的, 沒顯示的 alert／note 條數)。"""
    findings = list(findings or ())
    alerts = [f for f in findings if f.tone == ALERT]
    notes = [f for f in findings if f.tone == NOTE]
    goods = [f for f in findings if f.tone == GOOD]
    room = max(0, cap - len(alerts))
    shown = alerts + notes[:room]
    hidden = len(notes) - min(len(notes), room)
    if len(shown) < cap and goods:
        shown.append(goods[0])
    return shown, hidden


def readings_summary(rows, *, today: date, prev_week_ok: bool = False) -> str:
    """週私訊的讀數行（這 7 天；同一天同一個數字算一次）。沒有讀數回「這週沒有讀數」。"""
    rows = [row for row in (rows or ()) if isinstance(row, dict) and _row_day(row) is not None]
    week = _in(rows, today - timedelta(days=6), today)
    prev = _in(rows, today - timedelta(days=13), today - timedelta(days=7))
    parts = []
    bp = _readings(week, _BP)
    if bp:
        if len(bp) == 1:
            text = f"血壓 {bp[0]['value']}（{_md(bp[0]['day'])}）"
        else:
            systolic = _half_up(sum(r["parsed"][0] for r in bp), len(bp))
            diastolic = _half_up(sum(r["parsed"][1] for r in bp), len(bp))
            text = f"血壓 {len(bp)} 次平均 {systolic}/{diastolic}"
        prev_bp = _readings(prev, _BP) if prev_week_ok else []
        if len(prev_bp) == 1:
            text += f"（上週 {prev_bp[0]['value']}）"
        elif prev_bp:
            systolic = _half_up(sum(r["parsed"][0] for r in prev_bp), len(prev_bp))
            diastolic = _half_up(sum(r["parsed"][1] for r in prev_bp), len(prev_bp))
            text += f"（上週 {len(prev_bp)} 次平均 {systolic}/{diastolic}）"
        parts.append(text)
    for item in (_GLUCOSE, _HEART):
        values = _readings(week, item)
        if len(values) == 1:
            parts.append(f"{item} {values[0]['value']}（{_md(values[0]['day'])}）")
        elif values:
            low = min(values, key=lambda r: r["parsed"])["value"]
            high = max(values, key=lambda r: r["parsed"])["value"]
            parts.append(f"{item} {len(values)} 次 {low}–{high}")
    weights = _readings(week, _WEIGHT)
    if weights:
        parts.append(f"體重 {weights[-1]['value']}（{_md(weights[-1]['day'])}）")
    return "讀數：" + "、".join(parts) if parts else "這週沒有讀數"


# ── 每日檢查：很高的血壓、低血糖（當下怎麼做只在每日檢查，不在卡片） ─────────────


def urgent_readings(rows, *, since_ts: float) -> list[tuple[str, date, str, float]]:
    """since_ts 之後提到的危象血壓（>180/120）與低血糖（<70），每一筆都列（同一天同一個數字算一筆，時間取最早
    那則）→ [(kind, 讀數那天, 讀數, 時間)]。同一種裡最嚴重的在前：血壓收縮壓高的、再舒張壓高的；血糖低的；
    一樣嚴重取較新的。呼叫端（每日檢查）扣掉已提醒過的讀數後，每種只送第一筆（Andrew 10/18：「同一筆讀數只
    提醒一次，每天最多一則」）。"""
    rows = [row for row in (rows or ()) if isinstance(row, dict) and _row_day(row) is not None and _row_ts(row) >= since_ts]
    crisis = [r for r in _readings(rows, _BP, earliest=True) if r["parsed"][0] > 180 or r["parsed"][1] > 120]
    crisis.sort(key=lambda r: (-r["parsed"][0], -r["parsed"][1], -r["ts"]))
    low = [r for r in _readings(rows, _GLUCOSE, earliest=True) if r["parsed"] < 700]
    low.sort(key=lambda r: (r["parsed"], -r["ts"]))
    return [("bp_crisis", r["day"], r["value"], r["ts"]) for r in crisis] + [
        ("glucose_low", r["day"], r["value"], r["ts"]) for r in low
    ]


# 當下怎麼做：私訊 Andrew 和推到家族群組（2026-10-28）共用同一段，接在「請{誰}」後面
_URGENT_ADVICE = {
    "bp_crisis": "休息 5 分鐘後再量一次；還是很高要盡快就醫；有胸痛胸悶、呼吸困難、嘴歪或說話不清、單側手腳無力就打 119",
    "glucose_low": (
        "現在再量一次；還是低於 70 或有發抖、冒冷汗，意識清楚、能吞嚥的話先吃 15 克糖（約 3 顆方糖或 120cc 果汁），"
        "15 分鐘後再量，還是很低就送醫；意識不清或不能吞嚥不要餵，直接打 119"
    ),
}


def _urgent_reading(kind: str, value: str) -> str:
    """「血壓 185/125（高壓超過 180、低壓超過 120）」「血糖 62（低於 70）」；其他種類丟 ValueError。"""
    if kind == "bp_crisis":
        parsed = _parse_bp(value)
        parts = []
        if parsed and parsed[0] > 180:
            parts.append("高壓超過 180")
        if parsed and parsed[1] > 120:
            parts.append("低壓超過 120")
        return f"血壓 {value}（{'、'.join(parts) or '很高'}）"
    if kind == "glucose_low":
        return f"血糖 {value}（低於 70）"
    raise ValueError("unknown urgent kind")


def _said_at(event_ts: float) -> str:
    when = datetime.fromtimestamp(float(event_ts), _TPE)
    return f"{when.month}/{when.day} {when:%H:%M}"


def urgent_dm_text(role: str, pronoun: str, kind: str, value: str, event_ts: float) -> str:
    """每日檢查私訊 Andrew 的那行。時間是群組裡提到的時間（不一定是量的時間），也可能是家人轉述的。"""
    reading = _urgent_reading(kind, value)
    return f"{_said_at(event_ts)} 群組提到{role}的{reading}：請{pronoun}{_URGENT_ADVICE[kind]}"


def urgent_group_text(role: str, kind: str, value: str, event_ts: float) -> str:
    """每日檢查推到家族群組的那行（2026-10-28 私訊改推群組）：對全家說。每日檢查在晚上 9 點，讀數可能是早上
    提到的，所以寫「之後還沒再量過的話」才照做；當下怎麼做和私訊同一段。"""
    reading = _urgent_reading(kind, value)
    return f"{_said_at(event_ts)} 提到{role}的{reading}：之後還沒再量過的話，請{role}{_URGENT_ADVICE[kind]}"
