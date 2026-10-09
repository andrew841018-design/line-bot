# Synthetic fixtures compose fake clinician names explicitly; no real identities.
"""2026-10-04: unsearched "corrections" about a named person's health, and media names as proof.

A burst reply "corrected" a family member about a public figure's illness with a
made-up year and diagnosis, and when challenged answered that several papers
had reported it.  Neither reply had any search behind it.  Sentences that state
a named person's health／death／legal events, or cite outlets as evidence, now
need search grounding (or the research path's evidence, or the material the
user shared); otherwise they are dropped.  All names and fixtures are synthetic.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

import reply_provenance  # noqa: E402
import reply_policy  # noqa: E402

SOURCE = "王大明院長今年6月在家中倒地送醫，目前人還在昏迷中。"
FABRICATED = (
    "事實上，王大明院長在2019年因感染導致敗血性休克，目前已恢復意識，狀況穩定。\n\n"
    "王大明院長並非如訊息所說，而是接受了心臟支架手術。\n\n"
    "如果你對哪些食物有疑問，我可以幫你查證喔。"
)
MEDIA_CLAIM = "聯合報、自由時報等多家媒體當時均有報導此事件。"


@pytest.fixture(autouse=True)
def _clean_grounding():
    reply_provenance.reset()
    yield
    reply_provenance.reset()


def _strip(reply, **kwargs):
    return reply_policy.strip_unbacked_public_claims(reply, **kwargs)


def _grounded_response(*segments):
    meta = SimpleNamespace(
        grounding_chunks=[SimpleNamespace(web=SimpleNamespace(uri="https://example.test/a"))],
        grounding_supports=[SimpleNamespace(segment=SimpleNamespace(text=s)) for s in segments],
        web_search_queries=["synthetic query"],
    )
    return SimpleNamespace(candidates=[SimpleNamespace(grounding_metadata=meta)])


def _record(*segments):
    """What gemini_client records for a returned answer whose search supports ``segments``."""
    import gemini_client

    reply_provenance.record_grounding(
        gemini_client.extract_grounding(_grounded_response(*segments), model="gemini-test")
    )


def _answer_with_search(text):
    """A Gemini answer as _run returns it: searched, and its search supports ``text``."""

    def answer(*_a, **_kw):
        reply_provenance.mark_searched()
        _record(text)
        return text

    return answer


# ── reply_policy.strip_unbacked_public_claims ───────────────────────────────

def test_unsearched_correction_of_a_named_person_is_dropped_to_silence():
    # The offer that is left over is not an answer either.
    assert _strip(FABRICATED, source_text=SOURCE) == ("", 3)


def test_narrative_sentences_about_the_same_person_are_dropped_too():
    reply = (
        "實際事件發生於2019年，李院長當時因心臟驟停接受了手術治療。\n"
        "診斷結果為心臟驟停，原因確實是血管嚴重阻塞。\n"
        "他接受了緊急的心臟支架手術，術後恢復情況良好。\n\n"
        "平時規律運動、控制血壓，才是保護心血管的關鍵。"
    )
    out, dropped = _strip(reply, source_text="李志明院長今年6月倒地送醫，目前昏迷中")
    assert dropped == 3
    assert out == "平時規律運動、控制血壓，才是保護心血管的關鍵。"


@pytest.mark.parametrize("name", ["吳" "假名醫師", "吴志明醫師", "院長吳志明是", "董事長陳志明"])
def test_surname_led_names_next_to_a_title_are_anchors(name):
    reply = f"{name}在2019年因中風住院。"
    assert _strip(reply, source_text="") == ("", 1)


def test_a_sentence_that_opens_with_the_year_keeps_its_date():
    reply = "王大明院長上週出席活動。\n2019年曾因中風住院。\n多喝水、早點睡。"
    assert _strip(reply, source_text=SOURCE) == ("王大明院長上週出席活動。\n\n多喝水、早點睡。", 1)


def test_numbered_pronoun_sentence_is_still_flagged():
    reply = "1. 他接受了緊急的心臟支架手術。\n多喝水、早點睡。"
    assert _strip(reply, source_text=SOURCE) == ("多喝水、早點睡。", 1)


@pytest.mark.parametrize("reply", [
    MEDIA_CLAIM,
    "這是聯合報的報導，不是網路謠言。",
])
def test_named_outlets_cited_as_evidence_are_dropped(reply):
    assert _strip(reply, source_text="這是真的嗎") == ("", 1)


def test_generic_media_evidence_is_dropped_only_in_a_named_person_topic():
    person = "根據當時的報導，事件發生在2019年5月3日。"
    assert _strip(person, source_text=SOURCE) == ("", 1)
    weather = "根據報導，颱風明天會登陸。"
    assert _strip(weather, source_text="颱風要來了嗎") == (weather, 0)


# GP1 review I8: these ordinary health／news replies must survive without search.
@pytest.mark.parametrize("reply, source", [
    ("醫生通常會建議住院觀察兩三天，出院後要多休息。", ""),
    ("醫師一般會先安排抽血，手術前要空腹八小時。", ""),
    ("事實上，手術後的恢復期因人而異…", "李" "醫師說要開刀"),
    ("根據報導，颱風明天會登陸。", ""),
])
def test_gp1_ordinary_health_and_news_replies_are_kept(reply, source):
    assert _strip(reply, source_text=source) == (reply, 0)


@pytest.mark.parametrize("reply", [
    "醫生通常會建議住院觀察兩三天，出院後要多休息。",
    "醫師一般會先安排抽血，手術前要空腹八小時。",
    "事實上，手術後的恢復期因人而異，通常兩週可以出院。",
])
def test_generic_health_advice_is_kept_even_while_a_named_person_is_discussed(reply):
    assert _strip(reply, source_text=SOURCE) == (reply, 0)


# Phase 6 review (2026-10-05): a family member naming the family's own doctor
# in full must not turn every 他／她 health sentence into a claim about that
# doctor, or the family's question ends with no reply.
@pytest.mark.parametrize("source, reply", [
    ("李" "假名醫師說阿公要裝支架，要住多久", "他裝完支架後通常住院兩三天，之後回診追蹤就好。"),
    ("林" "假名醫師說媽媽下週要開刀", "她開刀前一晚要空腹八小時，手術後先住院觀察。"),
    ("陳" "假名醫師說爺爺下個月要做心臟手術", "他手術後可能要住院一週，記得帶換洗衣物。"),
    ("張" "假名醫師說奶奶要住院開刀", "她住院期間家人輪流陪伴，出院後飲食清淡就好。"),
    ("李" "假名醫師說阿公要裝支架，要住多久", "他裝完支架後要按時吃藥，避免再次中風。"),
    ("李" "假名醫師說阿公要裝支架，要住多久", "他裝支架是為了預防心肌梗塞。"),
    ("林" "假名醫師說媽媽下週要開刀", "她其實是很常見的手術，不用太擔心。"),
    # Naming the doctor without telling any event about them is not a story about the doctor.
    ("李" "假名醫師說阿公要裝支架，要住多久", "李" "假名醫師的安排很常見。\n他裝完支架後住院兩三天，之後回診追蹤。"),
])
def test_pronoun_care_sentences_after_a_named_family_doctor_are_kept(source, reply):
    assert _strip(reply, source_text=source) == (reply, 0)


@pytest.mark.parametrize("reply, kept", [
    # Only the pronoun ties these to the named person; each asserts something.
    ("他並不是在家中倒地，而是心肌梗塞昏迷。", ""),
    ("他並非跌倒，而是中風，建議大家多注意血壓。", ""),
    ("她多年前就已罹癌，一直在化療。", ""),
    # The reply itself told a named person's event; the next sentence continues it.
    ("王大明院長在家中倒地後送醫。\n他當時在客廳昏倒，送醫急救。", "王大明院長在家中倒地後送醫。"),
])
def test_pronoun_led_claims_about_the_named_person_are_still_dropped(reply, kept):
    assert _strip(reply, source_text=SOURCE) == (kept, 1)


# Phase 6 review r3 (2026-10-05): the date／status branch still flagged care
# answers once the family's own doctor was named in full.  A year or a status
# phrase counts only in a sentence that refers to a named person — the name,
# the surname with a title, or the reply's own account of their event — and
# never in care advice.
@pytest.mark.parametrize("source, reply", [
    # GP1 r3 probes
    ("林" "假名醫師說媽媽下週要開刀", "術後一般要住院三到五天，狀況穩定就能出院。"),
    ("黃" "假名醫師說阿嬤確診肺炎，要住院嗎", "肺炎通常要看血氧和發燒情況，狀況穩定的話醫師可能讓她回家吃藥。"),
    ("李" "假名醫師說阿公要裝支架，要住多久", "目前已經很常見，裝完支架通常住院兩三天。"),
    ("黃" "假名醫師說阿嬤確診肺炎，要住院嗎", "肺炎通常要住院一週左右，狀況穩定就能出院。"),
    ("李" "假名醫師說阿公要裝支架，要住多久", "裝支架通常住院兩三天，目前已是很成熟的手術，不用太擔心。"),
    # A year or a status phrase with nobody named in the sentence.
    ("林" "假名醫師說媽媽下週要開刀", "這種手術2019年就納入健保給付了。"),
    ("李" "假名醫師說阿公要裝支架，要住多久", "裝支架目前已經是很成熟的手術了。"),
    ("張" "假名醫師說奶奶上週開刀", "她住院這幾天目前已經可以下床走動了。"),
    ("黃" "假名醫師說阿嬤確診肺炎，要住院嗎", "阿嬤2019年也住院過一次，這次記得多休息。"),
    ("林" "假名醫師說媽媽10月20日要動手術", "10月20日開刀的話，前一晚記得空腹八小時。"),
    # 「事實上」 alone does not make a status a claim, nor a year in advice.
    ("李" "假名醫師說阿公要裝支架，要住多久", "事實上，裝支架目前已經是很成熟的手術了。"),
    ("林" "假名醫師說媽媽下週要開刀", "事實上，2019年以後這種手術通常只要住院三天。"),
    # The reply names the doctor: advice, a condition or comfort is still no claim.
    ("李" "假名醫師說阿公要裝支架，要住多久", "李" "醫師說的支架目前已經很普遍，通常住院兩三天。"),
    ("林" "假名醫師說媽媽下週要開刀", "林" "醫師會等狀況穩定再讓媽媽出院。"),
    ("林" "假名醫師說媽媽下週要開刀", "林" "醫師安排的手術目前已經很成熟，不用太擔心。"),
    # Naming the doctor without telling an event about them does not make the
    # next status sentence their story.
    ("李" "假名醫師說阿公要裝支架，要住多久", "李" "假名醫師的安排很常見。\n裝完支架目前已經可以很快出院。"),
])
def test_care_answers_with_a_year_or_status_after_a_named_family_doctor_are_kept(source, reply):
    assert _strip(reply, source_text=source) == (reply, 0)


@pytest.mark.parametrize("reply, kept", [
    # The bare name, or the surname with a title, ties the status or year to them.
    ("事實上，王大明目前已恢復意識，狀況穩定。", ""),
    ("王院長在2019年就曾因中風住院。", ""),
    ("王大明在5月3日已經出院了。", ""),
    # A correction asserts even with advice attached.
    ("事實上，王院長2019年就中風住院過，建議大家多注意血壓。", ""),
    # The reply itself told the named person's event; the status continues it.
    ("王大明院長在家中倒地後送醫。\n目前已恢復意識，狀況穩定。", "王大明院長在家中倒地後送醫。"),
    # Nobody named, but a dated correction of the user's account.
    ("實際事件發生於2019年，當時因心臟驟停住院。", ""),
])
def test_a_status_or_year_tied_to_the_named_person_is_still_dropped(reply, kept):
    assert _strip(reply, source_text=SOURCE) == (kept, 1)


# Phase 6 review r4 (2026-10-05): after r3's fix a status or death sentence
# with no subject of its own passed again.  Such a sentence continues the
# topic, and the topic is the person the user named — unless the user only
# cites that person as the one who said something (「某醫師說…」).
SOURCE_QUESTION = "王大明院長現在還在昏迷嗎？"


@pytest.mark.parametrize("source, reply", [
    # GP1 r4 probes
    (SOURCE, "目前已恢復意識，狀況穩定。"),
    (SOURCE, "已經過世了，家屬低調處理後事。"),
    (SOURCE_QUESTION, "目前已經恢復意識，轉到普通病房了。"),
    (SOURCE_QUESTION, "已經出院了，狀況穩定。"),
    # A frame, a 他, a bare title, an "after …" clause or a date in front of
    # the status still leaves the sentence without a subject of its own.
    (SOURCE, "事實上，目前已經脫離險境了。"),
    (SOURCE, "他目前已恢復意識，狀況穩定。"),
    (SOURCE, "院長目前已恢復意識，狀況穩定。"),
    (SOURCE, "院長的病情目前已經穩定，轉出加護病房。"),
    (SOURCE_QUESTION, "送醫後已經恢復意識，目前在普通病房觀察。"),
    (SOURCE_QUESTION, "10月3日已經出院了，狀況穩定。"),
    # Other words for a death.
    (SOURCE, "很遺憾，已經病逝了。"),
    (SOURCE, "已經往生了，家屬低調處理後事。"),
    # Another person cited as the speaker is no exemption, 說話 is no
    # citation, what the person said about themselves is about them, and
    # doing something for 我們 (not a relative) keeps them the subject.
    ("林" "假名醫師說王大明院長倒地送醫了", "目前已恢復意識，狀況穩定。"),
    ("王大明院長說話時突然倒地送醫，現在還在昏迷嗎？", "目前已經恢復意識了。"),
    ("王大明院長表示自己去年中風住院", "目前已經康復出院了。"),
    ("王大明院長幫我們爭取經費，今年6月倒地送醫", "目前已恢復意識，狀況穩定。"),
])
def test_a_status_with_no_subject_of_its_own_about_the_named_person_is_dropped(source, reply):
    assert _strip(reply, source_text=source) == ("", 1)


@pytest.mark.parametrize("source, reply", [
    # The doctor is only cited as the speaker (表示, a title before the name,
    # words in between) or treats a relative: the topic is the family
    # member or the operation.
    ("林" "假名醫師表示媽媽下週要開刀", "目前已經是很成熟的手術了。"),
    ("主治醫師林志明說阿公要裝支架", "目前已經是很常見的手術了，術後恢復也快。"),
    ("林" "假名醫師昨天跟我們說媽媽下週要開刀", "目前已經是很成熟的手術了。"),
    ("陳" "假名醫師也有提到爺爺要住院", "已經出院的病人要按時回診。"),
    ("林" "假名醫師在電話中跟我們說媽媽要開刀", "目前已經是很成熟的手術了。"),
    ("林" "假名醫師幫媽媽開刀，要住多久？", "目前已經是很成熟的手術了。"),
    # The status has a subject of its own, or is a condition.
    (SOURCE, "這類手術目前已經很成熟了。"),
    (SOURCE, "中風的治療目前已經進步很多，很多人都能康復。"),
    (SOURCE_QUESTION, "已經恢復意識的話，就能轉到普通病房。"),
])
def test_a_status_with_its_own_subject_or_after_a_doctor_cited_as_speaker_is_kept(source, reply):
    assert _strip(reply, source_text=source) == (reply, 0)


# The speaker and no-subject checks stay linear on long repetitive input.
@pytest.mark.parametrize("source, reply", [
    (SOURCE, "目前已" * 1500 + "恢復意識。"),
    (SOURCE, "，目前已" * 1500 + "恢復意識。"),
    (SOURCE, "送醫後" * 1500 + "已經恢復意識。"),
    (SOURCE, "後" * 3000 + "已經恢復意識。"),
    (SOURCE, "經過" * 1500 + "已經恢復意識。"),
    (SOURCE, "院長" * 1500 + "目前已恢復意識。"),
    (SOURCE, "1." * 1500 + "目前已恢復意識。"),
    ("林" "假名醫師說" * 1500, "目前已經是很成熟的手術了。"),
    ("林" "假名醫師" + "跟" * 3000 + "說媽媽要開刀", "目前已經是很成熟的手術了。"),
    ("林" "假名醫師" + "也" * 3000 + "說媽媽要開刀", "目前已經是很成熟的手術了。"),
    ("醫師林志明" * 1500 + "倒地送醫", "目前已恢復意識，狀況穩定。"),
])
def test_speaker_and_no_subject_checks_stay_fast(source, reply):
    start = time.perf_counter()
    _strip(reply, source_text=source)
    assert time.perf_counter() - start < 0.05


@pytest.mark.parametrize("reply", [
    "胸悶冒冷汗要立刻送醫，醫師會評估是否需要放支架。",
    "許" "假名醫師都會建議術後住院觀察。",
    "主任" "醫師說明了手術風險，術後要多休息。",
    "高雄市長也呼籲民眾，胸痛要盡快送醫。",
    "部分媒體在報導中過度聚焦名人八卦。",
    "我查了但目前並無主流媒體報導證實這件事。",
    "如果能提供是哪個媒體的報導，會比較好找。",
    "其實住院觀察是常規做法，不用太擔心。",
    "趕快帶去看醫生，老貓腎臟病很常見。",
])
def test_generic_titles_and_non_evidence_media_mentions_are_kept(reply):
    assert _strip(reply, source_text="王大明院長住院了") == (reply, 0)


def test_material_the_user_shared_backs_a_plain_statement_but_not_a_denial_or_new_detail():
    material = "李小華教授上週接受心導管手術，目前已出院休養。"
    plain = "報導中提到李小華教授接受了心導管手術，目前已出院。"
    assert _strip(plain, source_text="分享一篇新聞", material_text=material) == (plain, 0)
    new_detail = "李小華教授在2019年也曾因中風住院。"
    assert _strip(new_detail, source_text="分享一篇新聞", material_text=material) == ("", 1)
    denial = "王" "假名醫師並非如影片中的推論，是因為心肌梗塞而昏迷。"
    assert _strip(denial, source_text="王" "假名醫師心肌梗塞昏迷了") == ("", 1)


def test_generic_report_line_about_shared_material_is_kept():
    material = "王大明院長經過治療後已出院，院方表示狀況穩定。"
    reply = "根據報導，王大明院長已經出院，狀況穩定。"
    assert _strip(reply, source_text="分享新聞", material_text=material) == (reply, 0)


def test_search_grounding_must_cover_the_sentence_itself():
    sentence = "事實上，王大明院長在2019年因感染導致敗血性休克。"
    assert _strip(sentence, source_text=SOURCE, supported_segments=[sentence]) == (sentence, 0)
    unrelated = ["王大明院長是知名的腸胃科權威。"]
    assert _strip(sentence, source_text=SOURCE, supported_segments=unrelated) == ("", 1)


def test_research_evidence_backs_the_claim():
    sentence = "事實上，王大明院長在2019年因感染導致敗血性休克。"
    evidence = "（新聞）2019年王大明院長因感染併發敗血性休克住院治療"
    assert _strip(sentence, source_text=SOURCE, evidence_text=evidence) == (sentence, 0)
    assert _strip(sentence, source_text=SOURCE, evidence_text="王大明院長出席活動") == ("", 1)


def test_outlet_the_user_named_is_not_invented_evidence():
    reply = "這是法新社（AFP）的報導，不是官方宣傳。"
    source = "法新社（AFP）有影片 https://example.test/v"
    assert _strip(reply, source_text=source) == (reply, 0)


def test_findings_count_flagged_sentences_in_a_bot_reply():
    assert reply_policy.public_claim_findings(FABRICATED) == 2
    assert reply_policy.public_claim_findings(MEDIA_CLAIM) == 1
    assert reply_policy.public_claim_findings("醫生通常會建議住院觀察兩三天。") == 0
    assert reply_policy.public_claim_findings("燙青菜少油少鹽，比較適合控制血壓。") == 0


@pytest.mark.parametrize("text, expected", [
    ("咪寶 根本沒有這回事，錯誤百出", True),
    ("你說錯了，不是這樣", True),
    ("這是假的吧，我查不到", True),
    ("亂講，哪有這回事", True),
    ("咪寶 這個說法不錯", False),
    ("沒錯，就是這樣", False),
    ("我錯過了昨天的節目", False),
    ("這樣對不對？", False),
    ("咪寶 這是什麼意思", False),
])
def test_disputes_bot_claim(text, expected):
    assert reply_policy.disputes_bot_claim(text) is expected


# ── promise sentences the bot cannot keep (GP2 S2) ──────────────────────────

@pytest.mark.parametrize("reply", [
    "好的，到時我會提醒你。",
    "沒問題，10/23 我會提醒大家出發。",
    "好，記下了，到時候提醒你！",
    "我會在前一天提醒你帶護照。",
    "我會記得提醒你回診。",
    "好，會記得提醒。",
    "屆時會再提醒大家。",
    "幫你記住了，下週三回診。",
    "會再提醒您帶健保卡。",
    "如果需要，我會提醒你。",
])
def test_reminder_promises_are_dropped(reply):
    assert reply_policy.strip_operation_claims(reply)[0] == ""


@pytest.mark.parametrize("reply", [
    "記得提醒爸爸帶健保卡。",
    "建議設個鬧鐘提醒你自己。",
    "到時候記得帶外套。",
    "我會記得帶傘。",
    "手機會再提醒你一次，記得打開通知。",
    "要不要我到時提醒你？",
    "我沒辦法到時提醒你，建議設手機鬧鐘。",
    "你記下了嗎？",
])
def test_advice_questions_and_non_reminder_promises_are_kept(reply):
    assert reply_policy.strip_operation_claims(reply) == (reply, 0)


def test_promise_is_dropped_but_the_rest_of_the_reply_stays():
    reply = "牙醫回診前一晚不要吃太晚。到時我會提醒大家。"
    assert reply_policy.strip_operation_claims(reply) == ("牙醫回診前一晚不要吃太晚。", 1)


# GP1 r2 (S2): confirmations with no named subject, or 「記住了／會記得的」,
# still reached the family although no reminder was stored.
@pytest.mark.parametrize("reply", [
    "好的，10/23 和 10/25 都會提醒你。",
    "收到，會準時提醒你！",
    "好的，已經記住了！",
    "OK，我記住了，10/23 去看牙醫。",
    "了解，咪寶會記得的！",
    "沒問題，當天會提醒大家帶健保卡。",
    "好，也會提醒你。",
    "收到，會提醒的！",
    "好喔，咪寶已經記住了。",
    "放心，我一定會記住的。",
    "10/23 早上會提醒你喔！",
    "好喔～會提醒你",
])
def test_subjectless_and_remembered_promises_are_dropped(reply):
    assert reply_policy.strip_operation_claims(reply)[0] == ""


@pytest.mark.parametrize("reply", [
    "記得提醒爸爸帶健保卡。",
    "要記得吃藥。",
    "我記得上次是在台大。",
    "醫生會提醒你空腹時間。",
    "明天醫院會提醒你回診時間。",
    "你還記得那家餐廳嗎？",
    "我還記得小時候常去那裡。",
    "記好了，十點在門口集合。",
    "我想起來了，那家店週一公休。",
    "我會記得帶傘。",
    "不會提醒你，請自己設鬧鐘。",
    "會場會提醒大家入座。",
    "你記住了嗎？",
    "記住了，明天八點出發！",
    "鬧鐘會提醒你起床。",
    "當天診所會提醒你帶健保卡。",
    "醫院前一天會傳簡訊，當天也會提醒你報到。",
    "他看過一次，已經記住了。",
    "你給他看過了，會記得的。",
])
def test_advice_recall_and_third_party_reminders_survive(reply):
    assert reply_policy.strip_operation_claims(reply) == (reply, 0)


def test_new_promise_forms_drop_only_their_sentence():
    reply = "牙醫回診前一晚不要吃太晚。收到，會準時提醒你！"
    assert reply_policy.strip_operation_claims(reply) == ("牙醫回診前一晚不要吃太晚。", 1)


# GP2 r3 (2026-10-05): dates joined by "/" in a sentence that never says
# 會…提醒 made the promise pattern backtrack exponentially (11 dates took
# 1.6 s, ×4 per date) while holding the GIL, so every group's replies froze.
# A run of 剛 did the same to the 「剛剛已改成…」 pattern.
@pytest.mark.parametrize("text", [
    " / ".join(["10/23"] * 20) + " 要帶健保卡。",
    " / ".join(["10/23"] * 40) + " 要帶健保卡。",
    "/".join(["10/23"] * 40) + "要帶健保卡。",
    "/".join(["10月23日"] * 40) + "記得回診。",
    " ".join(["10月23日"] * 40) + "記得回診。",
    " / ".join(["10/23 早上 9:00"] * 20) + "回診。",  # privacy-safe-fixture
    " / ".join(["10:30"] * 40) + "出發。",
    "、".join(["三點半"] * 40) + "出發。",
    "/".join(["12"] * 40) + "號",
    "好，" * 40 + "10/23 / " * 40 + "出發。",
    "剛" * 60 + "改成明天。",
])
def test_promise_and_change_checks_stay_fast_on_long_repetitive_sentences(text):
    start = time.perf_counter()
    assert reply_policy.strip_operation_claims(text) == (text, 0)
    assert time.perf_counter() - start < 0.05


def test_a_long_date_list_that_ends_in_a_promise_is_still_dropped_quickly():
    text = " / ".join(["10/23"] * 40) + " 都會提醒你。"
    start = time.perf_counter()
    assert reply_policy.strip_operation_claims(text) == ("", 1)
    assert time.perf_counter() - start < 0.05


# GP1 r4 #4: a relative or vague time (月底, 幾天後, 下個月, 過幾天…) before
# 會…提醒, or right after 會, is the same promise; with no stored reminder
# behind it, 「好的！月底會提醒你繳房租喔～」 is a false promise.
@pytest.mark.parametrize("reply", [
    "好的！月底會提醒你繳房租喔～",
    "好喔～幾天後會提醒你回診。",
    "好的，月初會提醒你繳學費。",
    "收到，月中會提醒大家。",
    "好的，月底前會提醒你。",
    "好的，月底前一天會提醒你。",
    "好的，下個月底會提醒你。",
    "好的，十月十五號會提醒你。",
    "好的，二十幾號會提醒你。",
    "好的，一個半小時後會提醒你。",
    "好的，這個月底會提醒你。",
    "收到！之後每個月底都會提醒你。",
    "好的，下個月會提醒你。",
    "好的，下週會提醒你剪頭髮。",
    "沒問題，下禮拜三會提醒你。",
    "好的，週末會提醒你。",
    "好的，年底會提醒你換駕照。",
    "好喔，過幾天會提醒你。",
    "好的，3天後會提醒你回診。",
    "3天後會提醒你回診。",
    "好的，10分鐘後會提醒你關火。",
    "好的，半小時後會提醒你。",
    "好的，兩個禮拜後會提醒你。",
    "好的，十幾號會提醒你繳學費。",
    "好的，15號會提醒你。",
    "好的，改天會提醒你。",
    "好的，以後會提醒你。",
    "好的，下次會提醒你。",
    "好的，等一下會提醒你關火。",
    "好喔，每天早上會提醒你吃藥。",
    "好的，時間到會提醒你。",
    "好的，會在月底提醒你繳房租。",
    "好的，會在前一天提醒你。",
    "好的，會在幾天後再提醒你一次。",
])
def test_promises_with_relative_or_vague_times_are_dropped(reply):
    assert reply_policy.strip_operation_claims(reply)[0] == ""


@pytest.mark.parametrize("reply", [
    "月底銀行會提醒你繳費。",
    "幾天後診所會提醒你回診。",
    "下週醫院會提醒你回診時間。",
    "之後系統會提醒你。",
    "月曆會提醒你。",
    "每個人都會提醒你。",
    "前台會提醒你。",
    "下個月記得繳房租。",
    "月底記得提醒爸爸繳房租。",
    "過幾天再提醒爸爸一次。",
    "如果月底會提醒你就好了。",
    "下個月會不會提醒你？",
    "以前會提醒你的鬧鐘壞了。",
])
def test_relative_times_with_someone_else_advice_or_questions_are_kept(reply):
    assert reply_policy.strip_operation_claims(reply) == (reply, 0)


@pytest.mark.parametrize("text, expected", [
    pytest.param("月底" * 2000 + "要繳費。", None, id="month-end-run"),
    pytest.param("好，" + "幾天後" * 1500 + "回診。", None, id="few-days-run"),
    pytest.param("下個月" * 1500 + "繳費。", None, id="next-month-run"),
    pytest.param("每個月底" * 1200 + "繳費。", None, id="every-month-end-run"),
    pytest.param("十幾" * 2400 + "號", None, id="teens-run"),
    pytest.param("過" * 4800 + "幾天", None, id="guo-run"),
    pytest.param("每" * 4800 + "天", None, id="mei-run"),
    pytest.param("好，" + "3 " * 2400 + "天後", None, id="digit-space-run"),
    pytest.param("好，" + " " * 4800 + "天後", None, id="space-run"),
    pytest.param("好的，會在" + "月底" * 2400 + "繳費。", None, id="hui-zai-run"),
    pytest.param("好的，會在" + " " * 4800 + "提", None, id="hui-zai-space-run"),
    pytest.param("好，" * 40 + "下下週" * 1500 + "出發。", None, id="ack-week-run"),
    pytest.param("好，" + "幾天後" * 1500 + "會提醒你。", ("", 1), id="few-days-promise"),
    pytest.param("好的，會在" + "月底" * 2400 + "提醒你。", ("", 1), id="hui-zai-promise"),
])
def test_relative_time_promise_check_stays_fast_on_long_repetitive_text(text, expected):
    start = time.perf_counter()
    result = reply_policy.strip_operation_claims(text)
    assert time.perf_counter() - start < 0.05
    assert result == ((text, 0) if expected is None else expected)


# ── gemini_client: search only, and the prompt rule ─────────────────────────

def test_chat_tools_search_but_do_not_run_code():
    import gemini_client

    assert any(getattr(t, "google_search", None) is not None for t in gemini_client._TOOLS)
    assert all(getattr(t, "code_execution", None) is None for t in gemini_client._TOOLS)


def test_output_style_rule_forbids_unsearched_corrections_about_named_people():
    import gemini_client

    rule = gemini_client._OUTPUT_STYLE_RULE
    assert "具名真人" in rule and "媒體名稱當證據" in rule


def test_claude_gets_the_named_person_rule_without_search_wording():
    """H1: Claude has no search tool, so its copy of the rule names attached data only."""
    import claude_client
    import gemini_client

    text = "王大明院長今年住院了"
    gemini = gemini_client._build_system_instruction([], None, user_input=text)
    assert "沒有搜尋結果或使用者提供的資料作依據，就不要更正使用者的說法" in gemini
    system, _user = claude_client._build_cli_prompt(text, [], [], None)
    payload = claude_client._build_payload(text, [], [], None)
    for prompt in (system, payload["system"]):
        head = prompt.split(reply_policy.NO_SEARCH_CONTRACT)[0]
        assert "沒有本次附上的資料或使用者提供的資料作依據，就不要更正使用者的說法" in head
        assert "沒有搜尋結果" not in head


# ── reply_provenance: search details next to H1's flags ─────────────────────

def test_provenance_keeps_search_details_without_touching_its_flags():
    reply_provenance.mark_searched()
    reply_provenance.mark_dropped()
    _record("一句。")
    info = reply_provenance.grounding()
    assert info == {
        "model": "gemini-test", "urls": ["https://example.test/a"],
        "supported_segments": ["一句。"], "queries": ["synthetic query"],
    }
    assert reply_provenance.is_grounded(info)
    assert reply_provenance.take_grounding() == info
    assert reply_provenance.grounding() is None
    assert reply_provenance.searched() and reply_provenance.dropped()
    _record()
    assert not reply_provenance.is_grounded(reply_provenance.grounding())
    reply_provenance.reset()
    assert reply_provenance.grounding() is None
    assert not reply_provenance.searched() and not reply_provenance.dropped()


# ── main._enforce_new_value_reply ───────────────────────────────────────────

def test_enforce_drops_unsearched_claims_and_logs_counts_only(caplog):
    import main

    caplog.set_level(logging.INFO)
    outcome: dict = {}
    out = main._enforce_new_value_reply(
        FABRICATED, source_text=SOURCE, request_text=SOURCE, context=[], outcome=outcome,
    )
    assert out == ""
    assert outcome["public_claims_emptied"] is True
    assert outcome["recorded"] is False and outcome["grounded"] is False
    # Logs carry counts only, never the reply text.
    assert "敗血性休克" not in caplog.text
    assert "public-claim guard dropped=3" in caplog.text


def test_enforce_keeps_sentences_backed_by_the_recorded_search_and_resets_it():
    import main

    sentence = "事實上，王大明院長在2019年因感染導致敗血性休克。"
    _record(sentence)
    reply_provenance.mark_searched()
    outcome: dict = {}
    out = main._enforce_new_value_reply(
        sentence, source_text=SOURCE, request_text=SOURCE, context=[], outcome=outcome,
    )
    assert out == sentence
    assert outcome["grounded"] is True and outcome["recorded"] is True
    assert outcome["public_claims_emptied"] is False
    assert reply_provenance.grounding() is None
    # Only the search details are consumed; H1's flag stays for the caller.
    assert reply_provenance.searched() is True


def test_enforce_uses_research_evidence_and_trusted_replay():
    import main

    sentence = "事實上，王大明院長在2019年因感染導致敗血性休克。"
    evidence = "2019年王大明院長因感染併發敗血性休克"
    assert main._enforce_new_value_reply(
        sentence, source_text=SOURCE, request_text=SOURCE, context=[], evidence_text=evidence,
    ) == sentence
    assert main._enforce_new_value_reply(
        sentence, source_text=SOURCE, request_text=SOURCE, context=[], trusted_grounded=True,
    ) == sentence
    assert main._enforce_new_value_reply(
        sentence, source_text=SOURCE, request_text=SOURCE, context=[],
    ) == ""


def test_enforce_resets_grounding_even_for_an_empty_reply():
    import main

    _record("任何句子。")
    assert main._enforce_new_value_reply(
        "", source_text=SOURCE, request_text=SOURCE, context=[],
    ) == ""
    assert reply_provenance.grounding() is None


def test_a_pretend_search_drops_the_whole_reply_before_the_public_claim_guard():
    import main

    outcome: dict = {}
    reply = "我查了一下，王大明院長在2019年因感染導致敗血性休克。\n\n平時規律運動才是關鍵。"
    assert main._enforce_new_value_reply(
        reply, source_text=SOURCE, request_text=SOURCE, context=[], outcome=outcome,
    ) == ""
    # H1's check ran first and dropped it whole; the public-claim guard never
    # ran, so nothing asks for a search retry.
    assert outcome["public_claims_dropped"] == 0
    assert outcome["public_claims_emptied"] is False


# ── fact cache marker ───────────────────────────────────────────────────────

LONG_BURST = "王大明院長今年6月在家中倒地送醫，" * 6


def test_fact_cache_stores_only_grounded_replies_with_a_marker():
    import memory

    memory.store_fact_cache("GRP001", LONG_BURST, "沒有依據的回覆", False)
    assert memory.check_fact_cache("GRP001", LONG_BURST) is None
    memory.store_fact_cache("GRP001", LONG_BURST, "也沒有依據的回覆")
    assert memory.check_fact_cache("GRP001", LONG_BURST) is None
    memory.store_fact_cache("GRP001", LONG_BURST, "有搜尋依據的回覆", True)
    hit = memory.check_fact_cache("GRP001", LONG_BURST)
    assert hit == "有搜尋依據的回覆"
    assert hit.grounded is True


def test_unmarked_old_cache_rows_replay_as_ungrounded():
    import memory

    memory.store_fact_cache("GRP001", LONG_BURST, "舊回覆", True)
    with memory._conn() as c:
        c.execute("UPDATE fact_check_cache SET grounded=0")
    hit = memory.check_fact_cache("GRP001", LONG_BURST)
    assert hit == "舊回覆" and hit.grounded is False


def test_fact_cache_marker_column_is_added_to_an_old_table(monkeypatch, tmp_path):
    import memory

    db = tmp_path / "old.db"
    with sqlite3.connect(db) as c:
        c.execute(
            "CREATE TABLE fact_check_cache (group_id TEXT NOT NULL, text_hash TEXT NOT NULL,"
            " result TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,"
            " PRIMARY KEY (group_id, text_hash))"
        )
        c.execute(
            "INSERT INTO fact_check_cache VALUES (?, ?, ?, 0, 9999999999)",
            ("GRP001", memory._cache_key(LONG_BURST), "改版前的回覆"),
        )
    monkeypatch.setattr(memory, "_DB_PATH", db)
    memory._init_db()
    memory._init_db()  # a second process／restart must not fail on the existing column
    hit = memory.check_fact_cache("GRP001", LONG_BURST)
    assert hit == "改版前的回覆" and hit.grounded is False


# ── burst path ──────────────────────────────────────────────────────────────

def _burst_patches(monkeypatch, main, *, cached=None, llm_reply="", gemini_retry=None):
    from contextlib import nullcontext

    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda _name: False)
    monkeypatch.setattr(main.memory, "get_context", lambda _gid: [])
    monkeypatch.setattr(main.memory, "check_fact_cache", lambda *_a: cached)
    monkeypatch.setattr(main.memory, "top_facts", lambda _gid: [])
    monkeypatch.setattr(main, "_get_persona_notes", lambda _gid: [])
    monkeypatch.setattr(main, "_requires_public_research", lambda _t: False)
    monkeypatch.setattr(main, "_prefetch_urls", lambda text: text)
    monkeypatch.setattr(main, "_is_market_quote_request", lambda *_a, **_kw: False)
    monkeypatch.setattr(main, "_thinking_indicator", lambda _gid: nullcontext())
    monkeypatch.setattr(main, "_llm_chat", lambda *_a, **_kw: llm_reply)
    retry = MagicMock(side_effect=gemini_retry or (lambda *_a, **_kw: ""))
    monkeypatch.setattr(main, "_gemini_llm_chat", retry)
    store = MagicMock()
    monkeypatch.setattr(main.memory, "store_fact_cache", store)
    monkeypatch.setattr(main.memory, "append_turn", lambda *_a, **_kw: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *_a, **_kw: None)
    monkeypatch.setattr(main, "_start_burst_finance_extraction", lambda *_a: None)
    monkeypatch.setattr(main, "_maybe_extract_facts", lambda *_a, **_kw: None)
    monkeypatch.setattr(main, "_maybe_capture_calendar_event", lambda *_a, **_kw: None)
    finish = MagicMock()
    monkeypatch.setattr(main, "_finish_burst_without_reply", finish)
    silent = MagicMock(return_value=True)
    monkeypatch.setattr(main, "_mark_inbound_reply_completed_no_reply", silent)
    reply = MagicMock()
    monkeypatch.setattr(main, "_reply", reply)
    return SimpleNamespace(retry=retry, store=store, finish=finish, silent=silent, reply=reply)


def test_burst_replay_of_an_unmarked_cached_fabrication_is_still_guarded(monkeypatch):
    import main
    import memory

    cached = memory.FactCacheHit(FABRICATED, grounded=False)
    mocks = _burst_patches(monkeypatch, main, cached=cached)
    main._handle_burst_flush("GRP001", SOURCE, "TOKEN1", ["MSG1"])
    mocks.reply.assert_not_called()
    assert mocks.silent.call_count == 1


def test_burst_replay_of_a_marked_cache_entry_counts_as_grounded(monkeypatch):
    import main
    import memory

    sentence = "事實上，王大明院長在2019年因感染導致敗血性休克。"
    # A stale record from an earlier reply on this thread must not leak in.
    _record()
    reply_provenance.mark_searched()
    mocks = _burst_patches(monkeypatch, main, cached=memory.FactCacheHit(sentence, grounded=True))
    main._handle_burst_flush("GRP001", SOURCE, "TOKEN1", ["MSG1"])
    assert mocks.reply.call_args.args[1] == sentence
    assert reply_provenance.grounding() is None
    assert reply_provenance.searched() is False


def test_burst_retries_an_emptied_claude_reply_once_and_caches_only_if_grounded(monkeypatch):
    import main

    grounded_text = "王大明院長6月倒地後送醫急救，院方尚未公布最新病況。"

    mocks = _burst_patches(
        monkeypatch, main, llm_reply=FABRICATED, gemini_retry=_answer_with_search(grounded_text),
    )
    main._handle_burst_flush("GRP001", SOURCE, "TOKEN1", ["MSG1"])
    assert mocks.retry.call_count == 1
    assert mocks.reply.call_args.args[1] == grounded_text
    assert mocks.store.call_args.args[2:] == (grounded_text, True)


def test_burst_retry_that_is_still_unbacked_ends_silently(monkeypatch):
    import main

    mocks = _burst_patches(
        monkeypatch, main, llm_reply=FABRICATED, gemini_retry=lambda *_a, **_kw: FABRICATED,
    )
    main._handle_burst_flush("GRP001", SOURCE, "TOKEN1", ["MSG1"])
    assert mocks.retry.call_count == 1
    mocks.reply.assert_not_called()
    mocks.store.assert_not_called()
    assert mocks.finish.call_count == 1


def test_burst_ungrounded_reply_is_sent_but_not_cached_as_grounded(monkeypatch):
    import main

    mocks = _burst_patches(monkeypatch, main, llm_reply="燙青菜少油少鹽，比較適合控制血壓。")
    main._handle_burst_flush("GRP001", SOURCE, "TOKEN1", ["MSG1"])
    mocks.retry.assert_not_called()
    assert mocks.reply.call_count == 1
    assert mocks.store.call_args.args[3] is False


def test_burst_caches_a_searched_answer_even_without_supported_segments(monkeypatch):
    import main

    text = "燙青菜少油少鹽，比較適合控制血壓。"

    def gemini_answer(*_a, **_kw):
        reply_provenance.mark_searched()
        _record()  # a search fed it; no segment of this answer is marked supported
        return text

    mocks = _burst_patches(monkeypatch, main)
    monkeypatch.setattr(main, "_llm_chat", gemini_answer)
    main._handle_burst_flush("GRP001", SOURCE, "TOKEN1", ["MSG1"])
    assert mocks.reply.call_args.args[1] == text
    assert mocks.store.call_args.args[2:] == (text, True)


def test_no_search_retry_while_the_gemini_quota_flag_is_set(monkeypatch):
    """Review C4: only the tool-less last tier, lite or local would answer."""
    import time

    import main

    monkeypatch.setattr(main, "_quota_exhausted_until_ts", time.time() + 3600)
    mocks = _burst_patches(
        monkeypatch, main, llm_reply=FABRICATED,
        gemini_retry=_answer_with_search("王大明院長6月倒地後送醫急救，院方尚未公布最新病況。"),
    )
    main._handle_burst_flush("GRP001", SOURCE, "TOKEN1", ["MSG1"])
    mocks.retry.assert_not_called()
    mocks.reply.assert_not_called()
    mocks.store.assert_not_called()
    assert mocks.finish.call_count == 1


RETRY_WITH_SEARCH_CLAIM = "我查了一下，王大明院長6月倒地後送醫急救，院方尚未公布最新病況。"


def test_the_search_retry_may_say_it_searched_when_it_did(monkeypatch):
    import main

    mocks = _burst_patches(
        monkeypatch, main, llm_reply=FABRICATED,
        gemini_retry=_answer_with_search(RETRY_WITH_SEARCH_CLAIM),
    )
    main._handle_burst_flush("GRP001", SOURCE, "TOKEN1", ["MSG1"])
    assert mocks.retry.call_count == 1
    assert mocks.reply.call_args.args[1] == RETRY_WITH_SEARCH_CLAIM
    assert mocks.store.call_args.args[2:] == (RETRY_WITH_SEARCH_CLAIM, True)


def test_the_search_retry_is_checked_with_its_own_search(monkeypatch):
    import main

    def unsearched(*_a, **_kw):
        # Its segments would pass the public-claim guard, but no search ran.
        _record(RETRY_WITH_SEARCH_CLAIM)
        return RETRY_WITH_SEARCH_CLAIM

    mocks = _burst_patches(monkeypatch, main, llm_reply=FABRICATED, gemini_retry=unsearched)
    main._handle_burst_flush("GRP001", SOURCE, "TOKEN1", ["MSG1"])
    assert mocks.retry.call_count == 1
    mocks.reply.assert_not_called()
    mocks.store.assert_not_called()
    assert mocks.finish.call_count == 1


def test_the_search_retry_runs_as_caller_checked(monkeypatch):
    import main

    seen: list[bool] = []

    def retry(*_a, **_kw):
        seen.append(reply_provenance.caller_checks())
        return ""

    mocks = _burst_patches(monkeypatch, main, llm_reply=FABRICATED, gemini_retry=retry)
    main._handle_burst_flush("GRP001", SOURCE, "TOKEN1", ["MSG1"])
    assert seen == [True]
    assert mocks.finish.call_count == 1


# ── explicit path: dispute hook and the Claude-empty retry ──────────────────

PRE_DEPLOY = 1_000
POST_DEPLOY = 3_000


def _explicit_event(text, quoted_id=None):
    from linebot.v3.webhooks import MessageEvent, TextMessageContent

    evt = MagicMock(spec=MessageEvent)
    kwargs = {"quotedMessageId": quoted_id} if quoted_id else {}
    evt.message = TextMessageContent(id="MSG900", text=text, quoteToken="qt", **kwargs)
    evt.source = SimpleNamespace(type="group", group_id="GRP001", user_id="U_TEST")
    evt.reply_token = "TOKEN900"
    return evt


def _run_explicit(monkeypatch, text, *, quoted=None, llm_reply="", llm_side_effect=None,
                  gemini_retry=None, record_error=None, reply_result=True, append_error=None):
    from contextlib import nullcontext

    import main
    import public_research

    monkeypatch.setattr(main, "_PUBLIC_CLAIM_GUARD_DEPLOYED_AT", 2_000)
    quoted_id = quoted["id"] if quoted else None
    raw = (quoted["user_id"], quoted["text"]) if quoted else None
    record = (
        {"group_id": "GRP001", "message_id": quoted_id, "user_id": quoted["user_id"],
         "text": quoted["text"], "created_at": quoted["created_at"]}
        if quoted else None
    )
    llm = MagicMock(return_value=llm_reply, side_effect=llm_side_effect)
    retry = MagicMock(side_effect=gemini_retry or (lambda *_a, **_kw: ""))
    no_search = MagicMock(side_effect=AssertionError("a dispute must never be searched"))
    # One recorder for the order of memory writes and LINE replies.
    order = MagicMock()
    order.reply.return_value = reply_result
    order.append_turn.side_effect = append_error
    with (
        patch("main.memory.get_raw_message", return_value=raw),
        patch("main.memory.get_raw_message_record", return_value=record, side_effect=record_error),
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._build_quoted_block", return_value=""),
        patch("main._requires_public_research", return_value=False),
        patch("main._get_explicit_market_quote_reply", return_value=None),
        patch("main._prefetch_urls", side_effect=lambda t: t),
        patch("main._thinking_indicator", side_effect=lambda *_a, **_kw: nullcontext()),
        patch("main._collect_web_research_sources", no_search),
        patch.object(public_research, "collect", no_search),
        patch("main._llm_chat", llm),
        patch("main._gemini_llm_chat", retry),
        patch("main.memory.append_turn", order.append_turn) as append_turn,
        patch("main._append_bot_turn", order.append_bot) as append_bot,
        patch("main._try_save_correction"),
        patch("main._maybe_extract_facts"),
        patch("main._maybe_capture_calendar_event"),
        patch("main._mark_inbound_reply_completed_no_reply") as mark_silent,
        patch("main._reply", order.reply) as mock_reply,
    ):
        main._handle_explicit_text(_explicit_event(text, quoted_id), "GRP001", text)
    no_search.assert_not_called()
    return SimpleNamespace(
        llm=llm, retry=retry, reply=mock_reply, silent=mark_silent,
        append_turn=append_turn, append_bot=append_bot, order=order,
    )


def _quote(text, created_at, user_id="__bot__"):
    return {"id": "QBOT1", "user_id": user_id, "text": text, "created_at": created_at}


@pytest.mark.parametrize("quoted_text", [FABRICATED, MEDIA_CLAIM])
def test_dispute_of_a_pre_deploy_claim_gets_the_fixed_retraction(monkeypatch, quoted_text):
    import main

    run = _run_explicit(
        monkeypatch, "根本沒有這回事，錯誤百出", quoted=_quote(quoted_text, PRE_DEPLOY),
    )
    run.llm.assert_not_called()
    run.retry.assert_not_called()
    assert run.reply.call_args.args[1] == main._DISPUTED_CLAIM_RETRACTION
    assert main._DISPUTED_CLAIM_RETRACTION == "我先前那則說法查不到可靠來源支持，先收回，請以正式報導為準。"
    # The disputed claim itself is not written back into the conversation.
    remembered = " ".join(str(c.args) for c in run.append_turn.call_args_list)
    assert "敗血性休克" not in remembered and "自由時報" not in remembered


def test_dispute_of_a_post_deploy_reply_is_not_retracted(monkeypatch):
    run = _run_explicit(
        monkeypatch, "根本沒有這回事，錯誤百出",
        quoted=_quote(FABRICATED, POST_DEPLOY), llm_reply="燙青菜少油少鹽，比較適合控制血壓。",
    )
    run.llm.assert_called_once()
    assert run.reply.call_args.args[1] == "燙青菜少油少鹽，比較適合控制血壓。"


@pytest.mark.parametrize("quoted_text", [
    "@爸爸\n⏰ 提醒（明天）\n2026-10-03 17:30 王朝餐廳用餐",
    "🔔 **明天活動提醒**\n📅 2026-10-08 14:00\n🎯 王大明院長手術後回診",  # privacy-safe-fixture
    "已新增提醒\n時間：2026-10-08 15:00\n事項：王大明院長在2019年手術回診",  # privacy-safe-fixture
    "{p1}\n⏰ 提醒（1 小時後）\n2026-10-08 14:00 陳" "假名醫師門診手術",  # privacy-safe-fixture
    "找到「王大明」相關對話：\n1. 10/1 王大明院長在2019年住院過",
    "目前待辦/提醒：\n提醒事項：\n1. 2026-10-08 陳" "假名醫師回診手術",
    "【市場報價】合成指數 2019年 收盤",
    "燙青菜少油少鹽，比較適合控制血壓。",
])
def test_dispute_of_pushes_receipts_or_unflagged_replies_takes_the_normal_path(
    monkeypatch, quoted_text,
):
    import main

    run = _run_explicit(
        monkeypatch, "你說錯了", quoted=_quote(quoted_text, PRE_DEPLOY),
        llm_reply="早上空腹量血壓比較準。",
    )
    run.llm.assert_called_once()
    assert run.reply.call_args.args[1] != main._DISPUTED_CLAIM_RETRACTION


def test_dispute_quoting_a_family_member_takes_the_normal_path(monkeypatch):
    run = _run_explicit(
        monkeypatch, "你說錯了", quoted=_quote(FABRICATED, PRE_DEPLOY, user_id="U_FAMILY"),
        llm_reply="早上空腹量血壓比較準。",
    )
    run.llm.assert_called_once()


def test_no_dispute_words_takes_the_normal_path(monkeypatch):
    run = _run_explicit(
        monkeypatch, "這是什麼意思", quoted=_quote(FABRICATED, PRE_DEPLOY),
        llm_reply="早上空腹量血壓比較準。",
    )
    run.llm.assert_called_once()


# Phase 6 review (2026-10-05): the dispute check must never abort the handler,
# and a retraction the group never saw must not enter the conversation memory.
@pytest.mark.parametrize("error", [
    sqlite3.OperationalError("database is locked 敗血性休克"),
    RuntimeError("synthetic failure 敗血性休克"),
])
def test_a_failed_dispute_check_takes_the_normal_path_and_logs_the_error_type_only(
    monkeypatch, caplog, error,
):
    caplog.set_level(logging.INFO)
    run = _run_explicit(
        monkeypatch, "根本沒有這回事，錯誤百出", quoted=_quote(FABRICATED, PRE_DEPLOY),
        record_error=error, llm_reply="早上空腹量血壓比較準。",
    )
    run.llm.assert_called_once()
    assert run.reply.call_args.args[1] == "早上空腹量血壓比較準。"
    assert type(error).__name__ in caplog.text
    assert "敗血性休克" not in caplog.text


def test_retraction_is_remembered_only_after_the_reply_went_out(monkeypatch):
    import main

    run = _run_explicit(
        monkeypatch, "根本沒有這回事，錯誤百出", quoted=_quote(FABRICATED, PRE_DEPLOY),
    )
    assert [c[0] for c in run.order.mock_calls] == ["reply", "append_turn", "append_bot"]
    assert run.append_turn.call_args.args == ("GRP001", "user", "（不確定是誰）：根本沒有這回事，錯誤百出")
    assert run.append_bot.call_args.args == ("GRP001", main._DISPUTED_CLAIM_RETRACTION)


def test_undelivered_retraction_is_not_remembered_or_answered_again(monkeypatch):
    import main

    run = _run_explicit(
        monkeypatch, "根本沒有這回事，錯誤百出", quoted=_quote(FABRICATED, PRE_DEPLOY),
        reply_result=False,
    )
    assert run.reply.call_count == 1
    assert run.reply.call_args.args[1] == main._DISPUTED_CLAIM_RETRACTION
    run.append_turn.assert_not_called()
    run.append_bot.assert_not_called()
    run.llm.assert_not_called()


def test_a_failed_memory_write_after_the_retraction_is_not_answered_again(monkeypatch, caplog):
    import main

    caplog.set_level(logging.INFO)
    run = _run_explicit(
        monkeypatch, "根本沒有這回事，錯誤百出", quoted=_quote(FABRICATED, PRE_DEPLOY),
        append_error=sqlite3.OperationalError("database is locked"),
    )
    assert run.reply.call_count == 1
    assert run.reply.call_args.args[1] == main._DISPUTED_CLAIM_RETRACTION
    run.llm.assert_not_called()
    assert "OperationalError" in caplog.text


def test_explicit_retries_an_emptied_claude_reply_once(monkeypatch):
    grounded_text = "王大明院長6月倒地後送醫急救，院方尚未公布最新病況。"

    run = _run_explicit(
        monkeypatch, "他現在怎麼樣了", llm_reply=FABRICATED,
        gemini_retry=_answer_with_search(grounded_text),
    )
    assert run.retry.call_count == 1
    assert run.reply.call_args.args[1] == grounded_text


def test_explicit_retry_that_is_still_unbacked_ends_silently(monkeypatch):
    run = _run_explicit(
        monkeypatch, "他現在怎麼樣了", llm_reply=FABRICATED,
        gemini_retry=lambda *_a, **_kw: FABRICATED,
    )
    assert run.retry.call_count == 1
    run.reply.assert_not_called()
    assert run.silent.call_count == 1


def test_explicit_retry_failure_ends_silently(monkeypatch):
    def broken(*_a, **_kw):
        raise RuntimeError("synthetic outage")

    run = _run_explicit(monkeypatch, "他現在怎麼樣了", llm_reply=FABRICATED, gemini_retry=broken)
    assert run.retry.call_count == 1
    run.reply.assert_not_called()
    assert run.silent.call_count == 1


def test_explicit_has_no_search_retry_while_the_gemini_quota_flag_is_set(monkeypatch):
    import time

    import main

    monkeypatch.setattr(main, "_quota_exhausted_until_ts", time.time() + 3600)
    run = _run_explicit(
        monkeypatch, "他現在怎麼樣了", llm_reply=FABRICATED,
        gemini_retry=_answer_with_search("王大明院長6月倒地後送醫急救，院方尚未公布最新病況。"),
    )
    run.retry.assert_not_called()
    run.reply.assert_not_called()
    assert run.silent.call_count == 1


def test_no_retry_when_the_reply_came_from_gemini(monkeypatch):
    def gemini_reply(*_a, **_kw):
        # Gemini answered (its search details are recorded) but did not search.
        _record()
        return FABRICATED

    run = _run_explicit(monkeypatch, "他現在怎麼樣了", llm_side_effect=gemini_reply)
    run.retry.assert_not_called()
    run.reply.assert_not_called()
    assert run.silent.call_count == 1


def test_no_retry_when_restatement_policy_emptied_the_reply(monkeypatch):
    run = _run_explicit(monkeypatch, SOURCE, llm_reply="確實，" + SOURCE)
    run.retry.assert_not_called()
    run.reply.assert_not_called()


# ── research path passes its sources as evidence ────────────────────────────

# No health word in the question: since H1 (2026-10-04) 住院／病名 keep a
# message out of the research path (public_research._PRIVATE).
RESEARCH_QUESTION = "今年王大明院長的新聞是真的嗎"
PERSON_SOURCES = [{
    "title": "合成新聞", "url": "https://example.test/news", "published": "2026-09-30",
    "full_text": "（合成新聞）2019年王大明院長因感染併發敗血性休克住院治療，院方表示目前已出院。",
}]


@pytest.fixture
def research_env(monkeypatch):
    from contextlib import nullcontext

    import main

    monkeypatch.setattr(main, "_thinking_indicator", lambda *_: nullcontext())
    monkeypatch.setattr(main.memory, "get_context", lambda *_: [])
    monkeypatch.setattr(main.memory, "top_facts", lambda *a, **k: [])
    monkeypatch.setattr(main.memory, "append_turn", lambda *a: None)
    monkeypatch.setattr(main, "_get_persona_notes", lambda *_: [])
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_: False)
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _text: PERSON_SOURCES)
    replies: list[str] = []
    monkeypatch.setattr(main, "_reply", lambda _token, text, **_kw: replies.append(text))
    return replies


@pytest.mark.parametrize("answer, sent", [
    ("事實上，王大明院長在2019年因感染導致敗血性休克住院。", True),
    ("事實上，王大明院長在2023年因中風住院。", False),
])
def test_research_answer_needs_the_collected_sources(monkeypatch, research_env, answer, sent):
    import main

    monkeypatch.setattr(main, "_llm_chat", lambda *_a: answer)
    event = SimpleNamespace(source=SimpleNamespace(user_id="U_TEST"), reply_token="T_TEST")
    assert main._handle_web_research_question(event, "GRP001", RESEARCH_QUESTION)
    assert research_env == ([answer] if sent else [])


def test_enforce_checks_exactly_the_text_that_can_be_sent():
    """2026-10-05: the guard cuts long model output once, before every check."""
    import time as _time

    import main

    claim = "提醒已更新"
    degenerate = claim * 4000 + "如果"
    started = _time.perf_counter()
    out = main._enforce_new_value_reply(
        degenerate, source_text="", request_text="", context=[], addressed=True,
    )
    assert _time.perf_counter() - started < 1.0
    assert len(out) <= main._GUARDED_REPLY_MAX_CHARS
    assert claim not in out
