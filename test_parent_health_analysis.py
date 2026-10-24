"""爸媽健康分析（parent_health_analysis，2026-10-17）的純函式測試：全部合成列，不碰 DB。"""

from __future__ import annotations

import importlib.util
import itertools
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import output_validator
import parent_health_analysis as pa

TPE = ZoneInfo("Asia/Taipei")
TODAY = date(2026, 10, 17)  # 週六
_ids = itertools.count(1)


def row(days_ago, category, item, value="", flag="", hour=12, minute=0):
    day = TODAY - timedelta(days=days_ago)
    ts = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TPE).timestamp()
    return {
        "event_id": next(_ids), "category": category, "item": item, "value": value, "flag": flag,
        "day": day, "event_ts": ts, "message_id": f"M{next(_ids)}",
    }


def bp(days_ago, value, **kw):
    return row(days_ago, "body", "血壓", value, **kw)


def kinds(findings):
    return [f.kind for f in findings]


def by_kind(findings, kind):
    matches = [f for f in findings if f.kind == kind]
    assert len(matches) == 1, kinds(findings)
    return matches[0]


def analyze(rows, **kw):
    return pa.analyze(rows, today=TODAY, **kw)


# ── 沒提到不推論 ────────────────────────────────────────────────────────────


def test_nothing_mentioned_gives_nothing():
    assert analyze([]) == []
    assert analyze([row(0, "exercise", "散步")]) == []  # 只運動 1 天：不會說「沒運動」
    assert analyze([row(1, "med", "吃藥"), row(0, "sleep", "睡得好")]) == []


def test_malformed_rows_and_values_are_ignored():
    rows = [
        bp(0, "abc"), bp(0, "300/20"), bp(0, "80/130"), bp(1, "185"),  # 形狀、範圍、收縮壓≤舒張壓
        row(0, "body", "血糖", "62.55"), row(0, "body", "心跳", "x"), row(0, "body", "體重", "10"),
        {"category": "body", "item": "血壓", "value": "190/100"},  # 沒有 day
        "not a row", None,
    ]
    assert analyze(rows) == []
    assert pa.readings_summary(rows, today=TODAY) == "這週沒有讀數"
    assert pa.urgent_readings(rows, since_ts=0) == []


# ── 緊急字詞（GP1 C1） ──────────────────────────────────────────────────────


def test_urgent_red_flag_record_is_an_alert_without_119():
    findings = analyze([row(1, "body", "跌倒", flag="redflag")], pronoun="她")
    finding = by_kind(findings, "redflag")
    assert finding.tone == "alert" and finding.card == "10/16 提到跌倒"
    assert finding.advice == "可以問問她現在的狀況，也跟醫生說"
    shown, hidden = pa.card_lines(findings)
    assert shown == [finding] and hidden == 0
    assert all("119" not in text for f in findings for text in (f.card, f.advice))


def test_past_or_resolved_red_flags_are_not_symptoms():
    rows = [row(d, "body", "中風") for d in (0, 1, 2)] + [row(d, "body", "跌倒") for d in (3, 4, 5)]
    assert analyze(rows) == []  # 「我以前中風過」存成 flag=''：不是這週的症狀


def test_red_flags_each_label_latest_and_week_mode_window():
    rows = [row(10, "body", "跌倒", flag="redflag"), row(2, "body", "跌倒", flag="redflag"),
            row(9, "body", "胸痛胸悶", flag="redflag")]
    card = [f.card for f in analyze(rows) if f.kind == "redflag"]
    assert card == ["10/15 提到跌倒", "10/8 提到胸痛胸悶"]
    week = [f.dm for f in analyze(rows, week=True) if f.kind == "redflag"]
    assert week == ["⚠️ 10/15 提到跌倒"]  # 週私訊只看這 7 天，10 天前的不會連兩週都列


# ── 血壓 ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value, expected",
    [("180/100", None), ("181/100", "高壓超過 180"), ("150/120", None), ("150/121", "低壓超過 120"),
     ("190/125", "高壓超過 180、低壓超過 120")],
)
def test_bp_crisis_boundaries(value, expected):
    findings = analyze([bp(1, value)])
    crisis = [f for f in findings if f.kind == "bp_crisis"]
    if expected is None:
        assert not crisis
    else:
        assert crisis[0].card == f"10/16 量到血壓 {value}，{expected}"
        assert crisis[0].tone == "alert" and "119" not in crisis[0].card + crisis[0].advice


def test_bp_crisis_mentions_a_later_reading_and_takes_the_latest_crisis():
    findings = analyze([bp(5, "190/100"), bp(3, "185/95"), bp(1, "140/85", hour=8)])
    assert by_kind(findings, "bp_crisis").card == "10/14 量到血壓 185/95，高壓超過 180（之後 10/16 量 140/85）"


@pytest.mark.parametrize(
    "values, fires",
    [(["129/79"] * 3, False), (["130/79"] * 3, True), (["129/80"] * 3, True), (["150/95"] * 2, False)],
)
def test_bp_average_threshold_is_inclusive(values, fires):
    rows = [bp(i, v) for i, v in enumerate(values)]
    assert ("bp_high_avg" in kinds(analyze(rows))) is fires


def test_bp_average_needs_two_days_and_counts_a_repeated_reading_once():
    same_day = [bp(0, "150/95", hour=h) for h in (8, 12, 20)]
    assert "bp_high_avg" not in kinds(analyze(same_day))
    repeated = [bp(0, "150/95"), bp(0, "150/95", hour=13), bp(1, "150/95")]  # 家長和家人各說一次
    assert "bp_high_avg" not in kinds(analyze(repeated))  # 只有 2 次不同讀數
    assert "bp_high_avg" in kinds(analyze(repeated + [bp(2, "148/92")]))


def test_bp_average_rounds_half_up_not_bankers():
    rows = [bp(0, "130/80"), bp(1, "131/81"), bp(2, "130/80"), bp(3, "131/81")]  # 平均 130.5/80.5
    finding = by_kind(analyze(rows), "bp_high_avg")
    assert finding.card == "血壓量了 4 次，平均 131/81，一般建議低於 130/80"
    assert finding.advice.startswith("回診帶紀錄問醫生；可以照 722 量")


def test_low_bp_needs_two_readings_or_dizziness_and_never_with_high_average():
    assert "bp_low" not in kinds(analyze([bp(1, "88/58")]))
    finding = by_kind(analyze([bp(1, "88/58"), row(0, "body", "頭暈")]), "bp_low")
    assert finding.card == "血壓有 1 次高壓低於 90，也提到頭暈"
    assert by_kind(analyze([bp(1, "88/58"), bp(2, "86/55")]), "bp_low").card == "血壓有 2 次高壓低於 90"
    # 舒張壓低、收縮壓正常不算低血壓；平均偏高時不會同時說偏低
    mixed = [bp(0, "142/58"), bp(1, "138/57"), bp(2, "145/62")]
    assert kinds(analyze(mixed)) == ["bp_high_avg"]
    swinging = [bp(0, "88/58"), bp(1, "160/100"), bp(2, "170/105"), row(0, "body", "頭暈")]
    assert "bp_high_avg" in kinds(analyze(swinging)) and "bp_low" not in kinds(analyze(swinging))
    assert "bp_low" not in kinds(analyze([bp(0, "120/55"), bp(1, "118/56")]))


# ── 血糖、心跳、體重 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("value, kind", [("70", None), ("69", "glucose_low"), ("199", None), ("200", "glucose_high")])
def test_glucose_boundaries(value, kind):
    findings = analyze([row(1, "body", "血糖", value)])
    assert kinds(findings) == ([kind] if kind else [])
    if kind == "glucose_low":
        assert findings[0].tone == "alert" and findings[0].advice == "可以問問現在的狀況，也跟醫生說"  # 不暗示自己調藥


def test_heart_rate_needs_two_readings_and_merges_both_sides():
    assert analyze([row(1, "body", "心跳", "105")]) == []
    assert by_kind(analyze([row(1, "body", "心跳", "105"), row(2, "body", "心跳", "110")]), "hr").card == "心跳有 2 次超過 100"
    both = [row(d, "body", "心跳", v) for d, v in ((1, "105"), (2, "101"), (3, "48"), (4, "45"))]
    assert by_kind(analyze(both), "hr").card == "心跳有 2 次超過 100、2 次低於 50"
    assert analyze([row(1, "body", "心跳", "100"), row(2, "body", "心跳", "50")]) == []


@pytest.mark.parametrize(
    "first, last, first_ago, last_ago, fires",
    [
        ("66.0", "62.7", 40, 1, True),   # 剛好 5%（浮點算會差一點點，要用整數比）
        ("66.0", "62.8", 40, 1, False),  # 4.8%
        ("60", "57", 100, 0, True),
        ("66.0", "62.0", 14, 1, False),  # 只隔 13 天
        ("66.0", "62.0", 40, 14, False),  # 最新一筆不在近 14 天
        ("62.0", "66.0", 40, 1, False),  # 變重不列
    ],
)
def test_weight_loss_boundaries(first, last, first_ago, last_ago, fires):
    rows = [row(first_ago, "body", "體重", first), row(last_ago, "body", "體重", last)]
    assert ("weight_loss" in kinds(analyze(rows))) is fires


def test_weight_loss_text_and_intentional_diet_is_skipped():
    rows = [row(40, "body", "體重", "66.0"), row(1, "body", "體重", "62.7")]
    assert by_kind(analyze(rows), "weight_loss").card == "體重從 66.0（9/7）到 62.7（10/16），少了 5%"
    assert analyze(rows + [row(30, "diet", "控制飲食")]) == []


# ── 用藥、症狀、睡眠、飲食 ───────────────────────────────────────────────────


def test_medicine_stop_and_forgot():
    assert by_kind(analyze([row(3, "med", "停藥")]), "med_stop").advice == "要確認是醫生說可以停的"
    assert analyze([row(1, "med", "忘了吃藥")]) == []
    forgot = by_kind(analyze([row(1, "med", "忘了吃藥"), row(3, "med", "忘了吃藥")]), "med_forgot")
    assert forgot.card == "這週說忘了吃藥 2 天" and forgot.advice == "可以試分格藥盒或手機鬧鐘"


def test_symptom_needs_three_distinct_days_and_uses_doctor_visits_only():
    two = [row(0, "body", "膝蓋痛"), row(0, "body", "膝蓋痛", hour=18), row(2, "body", "膝蓋痛")]
    assert analyze(two) == []
    three = two + [row(4, "body", "膝蓋痛")]
    assert by_kind(analyze(three), "symptom:膝蓋痛").advice == "可以安排看醫生"
    visit = by_kind(analyze(three, visits=[(TODAY + timedelta(days=7), "回診")]), "symptom:膝蓋痛")
    assert visit.advice == "10/24（六）回診時提"
    assert by_kind(analyze(three, visits=[(TODAY + timedelta(days=7), "看醫生")]), "symptom:膝蓋痛").advice == "10/24（六）看診時提"
    assert by_kind(analyze(three, visits=[(TODAY + timedelta(days=3), "看牙")]), "symptom:膝蓋痛").advice == "可以安排看醫生"
    assert by_kind(analyze(three, visits=[(TODAY + timedelta(days=15), "回診")]), "symptom:膝蓋痛").advice == "可以安排看醫生"
    assert by_kind(analyze(three, visits=[(TODAY + timedelta(days=14), "回診")]), "symptom:膝蓋痛").advice.startswith("10/31")


def test_symptom_labels_covered_by_reading_rules_are_not_repeated():
    high_words = [row(d, "body", "血壓高") for d in (0, 1, 2)]
    assert kinds(analyze(high_words)) == ["symptom:血壓高"]
    readings = [bp(0, "150/95"), bp(1, "148/92"), bp(2, "152/96")]
    assert kinds(analyze(high_words + readings)) == ["bp_high_avg"]


def test_sleep_diet_and_alcohol_thresholds():
    assert analyze([row(d, "sleep", "睡不好") for d in (0, 1, 2)]) == []
    sleep = by_kind(analyze([row(d, "sleep", "睡不好") for d in (0, 1, 2, 3)]), "sleep_bad")
    assert sleep.card == "7 天有 4 天睡不好" and "固定起床時間" in sleep.advice
    meals = analyze([row(1, "diet", "沒吃早餐"), row(3, "diet", "沒吃晚餐"), row(3, "diet", "沒吃午餐")])
    assert by_kind(meals, "diet_skip").card == "7 天有 2 天沒吃正餐"
    assert by_kind(analyze([row(1, "diet", "胃口不好"), row(2, "diet", "胃口不好")]), "appetite").advice == "持續的話跟醫生提"
    assert analyze([row(d, "diet", "喝酒") for d in (0, 1)]) == []
    drinking = analyze([row(d, "diet", "喝酒") for d in (0, 1, 2)] + [row(5, "diet", "戒酒")])
    assert kinds(drinking) == ["alcohol"]  # 喝酒 3 天時不會同時誇「有在戒酒」


def test_rows_outside_the_seven_day_window_do_not_count():
    rows = [row(d, "sleep", "睡不好") for d in (0, 1, 2, 7)]
    assert analyze(rows) == []


# ── 做得好的 ─────────────────────────────────────────────────────────────────


def test_good_exercise_and_strength_tip():
    walking = [row(d, "exercise", "散步") for d in (0, 2, 4)]
    finding = by_kind(analyze(walking), "good_exercise")
    assert finding.card == "這週 3 天有運動（散步），很好" and finding.advice == "再加點肌力、平衡運動更能防跌"
    assert by_kind(analyze(walking + [row(10, "exercise", "太極")]), "good_exercise").advice == ""
    assert by_kind(analyze(walking + [row(1, "exercise", "運動")]), "good_exercise").advice == ""
    assert analyze([row(d, "exercise", "散步") for d in (0, 2)]) == []


def test_good_medicine_and_diet():
    taken = [row(d, "med", "吃藥") for d in range(5)]
    assert by_kind(analyze(taken), "good_med").card == "這週 5 天說有吃藥，很規律"
    assert "good_med" not in kinds(analyze(taken + [row(6, "med", "忘了吃藥")]))
    assert by_kind(analyze([row(9, "diet", "少吃鹽"), row(2, "diet", "吃清淡")]), "good_diet").card == "有在吃清淡"


# ── 卡片條數 ─────────────────────────────────────────────────────────────────


def _rich_rows():
    return (
        [row(d, "sleep", "睡不好") for d in (0, 1, 2, 3)]
        + [row(d, "body", "膝蓋痛") for d in (0, 1, 2)]
        + [row(d, "med", "忘了吃藥") for d in (1, 2)]
        + [row(d, "diet", "喝酒") for d in (0, 1, 2)]
        + [row(d, "exercise", "散步") for d in (0, 2, 4)]
    )


def test_card_lines_cap_good_never_pushes_out_a_note():
    findings = analyze(_rich_rows())
    shown, hidden = pa.card_lines(findings)
    assert [f.kind for f in shown] == ["symptom:膝蓋痛", "med_forgot", "sleep_bad"] and hidden == 1
    two_notes = analyze([row(d, "sleep", "睡不好") for d in (0, 1, 2, 3)] + [row(d, "exercise", "散步") for d in (0, 2, 4)])
    shown, hidden = pa.card_lines(two_notes)
    assert [f.tone for f in shown] == ["note", "good"] and hidden == 0


def test_alerts_are_always_shown():
    rows = _rich_rows() + [row(1, "body", "跌倒", flag="redflag"), row(2, "body", "昏倒", flag="redflag"),
                           bp(3, "190/100"), row(4, "body", "血糖", "60")]
    shown, hidden = pa.card_lines(analyze(rows))
    assert [f.tone for f in shown] == ["alert"] * 4 and hidden == 4


# ── 週私訊 ───────────────────────────────────────────────────────────────────


def test_previous_week_counts_only_when_allowed():
    rows = [row(d, "sleep", "睡不好") for d in (0, 1, 2, 3, 8)]
    assert by_kind(analyze(rows, week=True), "sleep_bad").dm == "7 天有 4 天睡不好"
    assert by_kind(analyze(rows, week=True, prev_week_ok=True), "sleep_bad").dm == "7 天有 4 天睡不好（上週 1 天）"


def test_readings_summary():
    assert pa.readings_summary([], today=TODAY) == "這週沒有讀數"
    rows = [bp(0, "136/84"), bp(2, "134/84"), bp(9, "128/80"), bp(10, "128/80", hour=9),
            row(1, "body", "血糖", "98"), row(3, "body", "血糖", "150"), row(2, "body", "體重", "62.5"),
            row(0, "body", "心跳", "72")]
    assert pa.readings_summary(rows, today=TODAY, prev_week_ok=True) == (
        "讀數：血壓 2 次平均 135/84（上週 2 次平均 128/80）、血糖 2 次 98–150、心跳 72（10/17）、體重 62.5（10/15）"
    )
    assert pa.readings_summary(rows, today=TODAY).startswith("讀數：血壓 2 次平均 135/84、")


# ── 每日檢查的讀數私訊 ───────────────────────────────────────────────────────


def test_urgent_readings_since_and_text():
    rows = [bp(3, "200/110"), bp(1, "185/125", hour=8, minute=12), bp(0, "130/80"), row(0, "body", "血糖", "62")]
    since = datetime(2026, 10, 15, 21, 0, tzinfo=TPE).timestamp()
    found = pa.urgent_readings(rows, since_ts=since)
    assert [(kind, day, value) for kind, day, value, _ts in found] == [
        ("bp_crisis", date(2026, 10, 16), "185/125"), ("glucose_low", date(2026, 10, 17), "62"),
    ]
    text = pa.urgent_dm_text("爸爸", "他", "bp_crisis", "185/125", found[0][3])
    assert text == (
        "10/16 08:12 群組提到爸爸的血壓 185/125（高壓超過 180、低壓超過 120）：請他休息 5 分鐘後再量一次；"
        "還是很高要盡快就醫；有胸痛胸悶、呼吸困難、嘴歪或說話不清、單側手腳無力就打 119"
    )
    glucose = pa.urgent_dm_text("媽媽", "她", "glucose_low", "62", found[1][3])
    assert glucose == (
        "10/17 12:00 群組提到媽媽的血糖 62（低於 70）：請她現在再量一次；還是低於 70 或有發抖、冒冷汗，"
        "意識清楚、能吞嚥的話先吃 15 克糖（約 3 顆方糖或 120cc 果汁），15 分鐘後再量，還是很低就送醫；"
        "意識不清或不能吞嚥不要餵，直接打 119"
    )
    with pytest.raises(ValueError):
        pa.urgent_dm_text("爸爸", "他", "other", "1", 0)


# ── 文字與詞表 ───────────────────────────────────────────────────────────────


def _every_rule_rows():
    return _rich_rows() + [
        row(1, "body", "跌倒", flag="redflag"), bp(5, "190/100"), bp(3, "150/95"), bp(2, "148/92"), bp(1, "152/96"),
        row(1, "body", "血糖", "60"), row(2, "body", "血糖", "230"), row(1, "body", "心跳", "105"),
        row(2, "body", "心跳", "110"), row(60, "body", "體重", "66.0"), row(1, "body", "體重", "61.0"),
        row(3, "med", "停藥"), row(1, "diet", "沒吃早餐"), row(2, "diet", "沒吃晚餐"), row(1, "diet", "胃口不好"),
        row(2, "diet", "胃口不好"), row(5, "diet", "少吃鹽"),
    ]


def test_every_template_passes_the_outbound_validator_unchanged_and_never_says_119_on_the_card():
    visits = [(TODAY + timedelta(days=5), "回診")]
    texts = []
    for week in (False, True):
        for findings in (analyze(_every_rule_rows(), week=week, visits=visits), analyze(_every_rule_rows(), week=week)):
            for f in findings:
                texts += [f"{pa.TONE_EMOJI[f.tone]} {f.card}", f.dm] + ([f"→ {f.advice}"] if f.advice else [])
                assert "119" not in f.card + f.advice
    texts.append(pa.readings_summary(_every_rule_rows(), today=TODAY, prev_week_ok=True))
    kinds_seen = {f.kind.split(":")[0] for f in analyze(_every_rule_rows())}
    assert kinds_seen >= {"redflag", "bp_crisis", "glucose_low", "glucose_high", "hr", "weight_loss", "med_stop",
                          "symptom", "med_forgot", "sleep_bad", "diet_skip", "appetite", "alcohol", "good_exercise"}
    for text in texts:
        result = output_validator.validate_outbound_text(text)
        assert result.ok and result.text == text, (text, result.reason)


def _fresh_extract(monkeypatch):
    spec = importlib.util.find_spec("parent_health_extract")
    if spec is None:
        pytest.skip("parent_health_extract.py 不在")
    fresh = importlib.util.module_from_spec(spec)  # 其他測試的 fixture 可能換過詞表；另外載一份原始的
    monkeypatch.setitem(sys.modules, spec.name, fresh)  # dataclass 要在 sys.modules 找得到模組；測完還原
    spec.loader.exec_module(fresh)
    return fresh


def test_labels_match_the_real_vocabulary(monkeypatch):
    fresh = _fresh_extract(monkeypatch)
    allowed = fresh.ALLOWED_ITEMS
    assert tuple(fresh.RED_FLAG_LABELS) == pa.RED_FLAG_LABELS
    classes = (pa.STRENGTH_BALANCE, pa.OTHER_EXERCISE, pa.GENERIC_EXERCISE)
    assert not any(a & b for a, b in itertools.combinations(classes, 2))
    assert set(allowed["exercise"]) <= set().union(*classes), set(allowed["exercise"]) - set().union(*classes)
    assert {"吃藥", "忘了吃藥", "停藥"} <= set(allowed["med"])
    assert "睡不好" in allowed["sleep"]
    assert {"沒吃早餐", "沒吃午餐", "沒吃晚餐", "沒吃飯", "胃口不好", "喝酒"} <= set(allowed["diet"])
    assert {"少吃鹽", "少吃油", "少吃甜", "吃清淡", "控制飲食", "戒糖", "戒酒"} <= set(allowed["diet"])
    assert {"血壓", "血糖", "體重", "心跳", "頭暈", "血壓高", "血壓低", "血糖高"} <= set(allowed["body"])


def test_import_has_no_side_effects(tmp_path):
    code = (
        "import sys, threading\n"
        "import parent_health_analysis\n"
        "assert [t.name for t in threading.enumerate()] == ['MainThread']\n"
        "bad = {'parent_health', 'parent_health_db', 'parent_health_extract', 'notify_discord', 'memory', 'main'}\n"
        "assert not bad & set(sys.modules), bad & set(sys.modules)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(pa.__file__).parent), "LINE_BOT_DISABLE_DOTENV": "1"}
    proc = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-2000:]


def test_templates_never_name_a_disease_or_suggest_changing_medicine():
    texts = []
    for week in (False, True):
        for f in analyze(_every_rule_rows(), visits=[(TODAY + timedelta(days=5), "回診")], week=week, pronoun="他"):
            if f.kind != "redflag":  # 緊急字詞那條本來就是詞表標籤（「提到跌倒」）
                texts += [f.card, f.advice, f.dm]
    for word in ("高血壓", "低血壓", "糖尿病", "低血糖", "心律不整", "失眠", "憂鬱", "心臟病", "調藥", "減藥", "停用", "診斷"):
        assert not any(word in text for text in texts), word


def test_bad_values_never_reach_the_log(caplog):
    caplog.set_level("DEBUG")
    rows = [bp(0, "185/125x"), row(0, "body", "血糖", "6x2"), bp(1, "999/999")]
    analyze(rows)
    pa.readings_summary(rows, today=TODAY)
    pa.urgent_readings(rows, since_ts=0)
    assert "185" not in caplog.text and "999" not in caplog.text


def test_other_scenarios_pass_the_validator_too():
    scenarios = [
        [bp(1, "88/58"), bp(2, "86/55"), row(0, "body", "頭暈")],  # bp_low
        [row(d, "med", "吃藥") for d in range(6)],  # good_med
        [row(d, "exercise", "運動") for d in (0, 2, 4)],  # 只有泛稱
    ]
    for rows in scenarios:
        findings = analyze(rows, pronoun="她")
        assert findings
        for f in findings:
            for text in [f"{pa.TONE_EMOJI[f.tone]} {f.card}", f.dm] + ([f"→ {f.advice}"] if f.advice else []):
                result = output_validator.validate_outbound_text(text)
                assert result.ok and result.text == text, (text, result.reason)


def test_generic_exercise_is_not_repeated_in_brackets():
    finding = by_kind(analyze([row(d, "exercise", "運動") for d in (0, 2, 4)]), "good_exercise")
    assert finding.card == "這週 3 天有運動，很好" and finding.advice == ""


def test_no_praise_for_regular_medicine_when_stopping_was_mentioned():
    rows = [row(d, "med", "吃藥") for d in range(5)] + [row(1, "med", "停藥")]
    assert kinds(analyze(rows)) == ["med_stop"]


def test_urgent_reading_time_is_the_first_mention():
    rows = [bp(0, "185/125", hour=8), bp(0, "185/125", hour=12)]  # 爸爸先說，家人後來轉述
    [(kind, _day, value, when)] = pa.urgent_readings(rows, since_ts=0)
    assert (kind, value) == ("bp_crisis", "185/125")
    assert datetime.fromtimestamp(when, TPE).hour == 8


def test_single_previous_week_reading_is_not_called_an_average():
    rows = [bp(0, "136/84"), bp(2, "134/84"), bp(9, "128/80")]
    assert pa.readings_summary(rows, today=TODAY, prev_week_ok=True) == "讀數：血壓 2 次平均 135/84（上週 128/80）"


def test_urgent_readings_lists_every_reading_most_severe_first():
    rows = [bp(1, "185/100", hour=9), bp(0, "200/110", hour=8), bp(0, "190/125", hour=10), bp(0, "185/100", hour=11),
            row(1, "body", "血糖", "65"), row(0, "body", "血糖", "60"), row(0, "body", "血糖", "65", hour=15)]
    found = [(kind, day.day, value) for kind, day, value, _ts in pa.urgent_readings(rows, since_ts=0)]
    assert found == [
        ("bp_crisis", 17, "200/110"), ("bp_crisis", 17, "190/125"), ("bp_crisis", 17, "185/100"),
        ("bp_crisis", 16, "185/100"),  # 同一個數字、不同天：是另一筆
        ("glucose_low", 17, "60"), ("glucose_low", 17, "65"), ("glucose_low", 16, "65"),
    ]


# ── 最近聽到的（last_heard，2026-10-23 PLAN A A2） ──────────────────────────


def heard(rows, category, **kw):
    return pa.last_heard(rows, category, TODAY, **kw)


def test_last_heard_counts_days_from_today_and_yesterday():
    today = heard([row(0, "exercise", "散步")], "exercise")
    assert (today.day, today.item, today.days_ago, today.days_14) == (TODAY, "散步", 0, 1)
    yesterday = heard([row(1, "exercise", "爬山")], "exercise")
    assert (yesterday.days_ago, yesterday.days_14) == (1, 1)


def test_last_heard_takes_the_latest_row_and_counts_distinct_days_in_14():
    rows = [
        row(13, "exercise", "散步"),  # D-13：14 天內的第一天
        row(14, "exercise", "散步"),  # D-14：14 天外，只當上次聽到的參考
        row(3, "exercise", "散步", hour=8),
        row(3, "exercise", "太極", hour=18),  # 同一天兩筆：最後說的是太極、天數算一天
    ]
    found = heard(rows, "exercise")
    assert (found.day, found.item, found.days_ago, found.days_14) == (TODAY - timedelta(days=3), "太極", 3, 2)


def test_last_heard_outside_14_days_still_says_when():
    found = heard([row(20, "exercise", "散步")], "exercise")
    assert (found.days_ago, found.days_14) == (20, 0)


def test_never_heard_and_future_rows_are_none():
    assert heard([], "exercise") is None
    assert heard([row(0, "sleep", "睡不好")], "exercise") is None
    assert heard([row(-1, "exercise", "散步")], "exercise") is None  # 今天以後的列不算
    assert heard(["not a row", None, {"category": "exercise", "item": "散步"}], "exercise") is None  # 沒有 day


def test_forgetting_or_stopping_medicine_is_not_taking_it():
    rows = [row(0, "med", "忘了吃藥"), row(1, "med", "停藥"), row(2, "med", "拿藥")]
    assert heard(rows, "med", items={"吃藥"}) is None
    found = heard(rows + [row(5, "med", "吃藥")], "med", items={"吃藥"})
    assert (found.item, found.days_ago) == ("吃藥", 5)


def test_blood_pressure_only_counts_rows_with_a_reading_and_never_red_flags():
    rows = [
        row(0, "body", "跌倒", flag="redflag"),  # 紅旗列
        row(0, "body", "血壓高"),  # 症狀，不是讀數
        bp(0, "abc"), bp(1, "80/130"),  # 讀數不合格
        row(1, "body", "血壓", ""),  # 沒有數字
        bp(4, "135/85"),
    ]
    found = heard(rows, "body", items={"血壓"})
    assert (found.value, found.days_ago, found.days_14) == ("135/85", 4, 1)
    assert heard([row(0, "body", "跌倒", flag="redflag")], "body") is None
    assert heard([bp(0, "190/120", flag="redflag")], "body", items={"血壓"}) is None
