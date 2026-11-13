"""Replies that only hand the question back (2026-11-12, maintenance report D).  Synthetic data only.

11/12 a photo the bot could not see came back as a search-it-yourself-or-ask-the-maker line; 11/10, with nobody
asking it, the bot said it had no feature to remind someone.  ``reply_policy.strip_missing_data`` drops such a sentence only
when nothing else is in it, and never one about care.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import main
import reply_policy
from test_support_timing import cpu_seconds


@pytest.mark.parametrize("text", [
    # the 11/12 shape: the question handed to a search and to the seller
    "若您想了解「某某膠囊」的功效，建議上網搜尋它的成分和用量，或直接詢問廠商。",
    # the same shape, with the opening a restatement judge would cut
    "我看不到這張圖片，無法確認劑量資訊。\n若想知道效果，建議上網搜尋成分，或直接問廠商。",
    "這張圖我這邊沒收到，沒辦法判斷是哪一款。\n可以把圖上的文字貼出來嗎？",
    "看不到你傳的照片，所以沒辦法確認。",
    "咪寶目前看不到圖片喔。",
    "想知道更多的話，建議上網搜尋。",
    "請自行上網查詢相關資訊喔。",
    "建議直接向廠商確認。",
    "可以 Google 一下。",
    "建議搜尋這款的成分，或直接諮詢原廠。",  # no 上網／自己, and 諮詢 a seller
    # sellers in other words (review 1 #6)
    "建議詢問廠商或店家。", "可以問問店家或客服。", "建議直接詢問原廠客服。", "建議致電廠商。",
    "建議聯絡賣家。", "請洽原廠。", "建議詢問購買的店家。", "建議詢問經銷商。",
    # not seeing it in other words (review 1 #7)
    "我這邊目前看不到圖片。", "我目前還看不到圖片喔。", "抱歉看不到你傳的圖片。", "這邊看不到圖片喔。",
    "• 看不到你傳的圖片", "我看不到這張圖片的內容。", "好的，我看不到這張照片喔。", "收到，我這邊看不到照片。",
    "我沒收到照片，可以再傳一次嗎？", "我沒收到照片。可以再傳一次嗎？",
    # an acknowledgement, a heading or a lead-in does not open the reply (review 3 #4, #13)
    "收到囉～\n我看不到你傳的照片。\n這款一般一天兩顆，飯後吃。",
    "沒問題！我看不到你傳的照片。這款一般一天兩顆，飯後吃。",
    "好的，以下說明：\n我看不到你傳的照片。\n這款一般一天兩顆。",
    "好的喔！建議上網搜尋。", "收到喔！建議直接問廠商。",
    # the opening is the first sentence with something in it (review 1 #9)
    "好的！我看不到這張照片。不過一般這種藥一天三次，飯後吃。",
    "**重點**\n我看不到圖片。\n這款一天一顆，飯後吃。",
    "建議上網搜尋。我看不到圖片。這款一般一天一顆。",
    "可以  Google 一下。", "可以 GOOGLE 一下。", "自己可以上網搜尋看看。", "建議自己去網路上搜尋。",
])
def test_a_reply_that_only_hands_it_back_is_nothing(text):
    assert reply_policy.strip_missing_data(text, addressed=False) == ""
    assert reply_policy.strip_missing_data(text, addressed=True) == ""


NO_FEATURE = "咪寶目前沒有幫忙轉達的功能。\n這件事要請某人自己跟對方說一聲。"


def test_unasked_it_has_no_such_feature_is_nothing():
    text = NO_FEATURE
    assert reply_policy.strip_missing_data(text, addressed=False) == ""
    # asked 「你能不能…」, it is the answer; a caller that does not say who asked keeps it
    assert reply_policy.strip_missing_data(text, addressed=True) == text
    assert reply_policy.strip_missing_data(text) == text


@pytest.mark.parametrize("text, kept", [
    ("這款一般一天一顆，飯後吃。\n想知道更多的話，建議上網搜尋。", "這款一般一天一顆，飯後吃。"),
    # only a picture it cannot see, or a feature it lacks, takes the whole reply when it opens it
    ("建議上網搜尋相關資訊。\n這款一般一天一顆，飯後吃比較不傷胃。", "這款一般一天一顆，飯後吃比較不傷胃。"),
    ("先看保存期限和開封後能放多久。\n我沒看到那張照片，所以沒辦法判斷這款適不適合。", "先看保存期限和開封後能放多久。"),
    # a short answer is left when only a search or a seller line went (review 1 #5)
    ("可以退喔！建議先跟賣家確認。", "可以退喔！"),
    ("明天有開。建議上網搜尋。", "明天有開。"),
    ("• 建議上網搜尋\n• 或直接問廠商\n• 這款一天一顆", "• 這款一天一顆"),
    # a two-character answer still opens the reply (review 3 #4)
    ("可以。我沒有這家店的最新資料。不過一般超商都收這種發票，拿去櫃檯就行。", "可以。不過一般超商都收這種發票，拿去櫃檯就行。"),
    # a scam mentioned two sentences up does not keep a seller line (review 3 #10)
    ("這部劇的詐騙橋段在第五集。\n第六集比較普通。\n建議直接詢問客服。", "這部劇的詐騙橋段在第五集。\n第六集比較普通。"),
])
def test_a_hand_back_further_down_goes_alone(text, kept):
    assert reply_policy.strip_missing_data(text, addressed=False) == kept


@pytest.mark.parametrize("text", [
    "可以搜尋「某某步道」的路況回報再出發。",  # a named place to look
    "可以上網查詢公路局網站的即時路況。",
    "建議先查詢健保署網站的補助資格，再預約。",  # without 自己／上網 a lookup is a pointer
    "可以問問門市有沒有現貨。",  # what to ask is the answer
    "直接打電話問店家最準，順便問：\n1. 有沒有停車位\n2. 幾點打烊",
    "如果想反映，可以拍下照片和時間，直接告訴店員。",
    "依規定七天內可以退貨，但拆封的貼身用品可能例外，建議先詢問賣家。",
    "我沒看到照片裡有標價，不過這類商品一般一盒三百元上下。",
    "看不到圖片，建議直接請教藥師。",  # care is safety advice, not a hand-back
    "若您想知道劑量，建議諮詢醫師或藥師。",
    "照片看不到可能是檔案過期了，請對方重傳一次。",  # a how-to: nobody's inability
    "老闆說他看不到那張照片，要我們重傳。",  # someone else
    "我們看不到照片的話就先別付款。",
    "這支影片我只拿到標題和描述，沒有字幕，所以內容我無法確認。",  # the link rules' note
    "我沒用過這個功能，不過設定頁裡可以關掉通知。",
    "我沒用過這個功能。不過設定頁裡可以關掉通知。",  # review 1 #3
    "我沒試過這個功能。一般在設定的通知裡可以關掉。",
    # what a search found, or content in the same clause (review 1 #2)
    "上網搜尋過的人都說這家很好吃。",
    "在網路上搜尋到的評價大多是五星。",
    "上網查證後確認這是假消息。",
    "我上網查詢後發現這款一天只能吃一顆。",
    "不能確定的話先別吃，建議直接問廠商。",
    "需要確認保存期限，也可以直接問店家。",
    "要確認保存期限和過敏原，也可以問店家。",
    "想知道有沒有現貨，可以問問門市。",
    # the bot asking the family (review 1 #4)
    "看不到照片？試試重開 LINE。",
    "沒看到照片嗎？往上滑一下就有。",
    # a model number or a platform is somewhere to look (review 1 #12)
    "可以搜尋 PTT 上的心得。",
    "可以搜尋型號 ABC-123 的說明書。",
    # fraud: calling the bank is what to do (review 2 #6)
    "你的卡可能被盜刷了。請致電客服。",
    "這是詐騙，請直接打給銀行客服，不要轉帳。",
    "可以退喔，建議先跟賣家確認。",  # review 3 #5: 可以 is the answer here
])
def test_everything_else_stays(text):
    assert reply_policy.strip_missing_data(text, addressed=False) == text


@pytest.mark.parametrize("text, asked", [
    ("建議直接詢問廠商。", "這個要問誰才知道？"),
    ("可以上網搜尋型號。", "這要用什麼關鍵字搜？"),
    ("照片我這邊也看不到。", "為什麼我看不到照片"),
    ("照片我這邊也沒收到。可能是超過保存期限，請對方再傳一次就好。", "我沒收到你們傳的照片"),
    ("建議上網搜尋產品型號。", "這要去哪搜？\n好喔"),  # the question is not the last line (review 4 #8)
    ("建議上網搜尋產品型號。", "這要去哪搜？"),
    ("我目前沒有幫忙設提醒的功能。可以用手機內建的提醒 App。", "這個群組有提醒功能嗎？"),  # review 2 #3
])
def test_asked_for_exactly_that_it_is_the_answer(text, asked):
    assert reply_policy.strip_missing_data(text, asked, addressed=False) == text
    assert reply_policy.strip_missing_data(text, "合成", addressed=False) == ""


@pytest.mark.parametrize("text, asked", [
    # a word in passing is not asking for it (review 1 #4, #8)
    ("我看不到這張圖片，無法確認劑量資訊。", "字太小我看不到，這個一天吃幾顆"),
    ("建議上網搜尋相關新聞確認。", "這個連結說的是真的嗎？"),
    ("建議直接詢問廠商。", "你週末跟誰去？"),
    ("可以自己查一下。", "我怎麼找不到遙控器"),  # review 2 #4, also the 11/06 rule
    ("可以自己查一下。", "這是今年的熱門關鍵字嗎"),  # review 3 #6
    ("可以自己查一下。", "遙控器怎麼找都找不到"),
    ("可以自己查一下。", "去哪找也找不到"),
    (NO_FEATURE, "我手機的鬧鐘功能怪怪的\n你幫我提醒對方明天回診"),  # review 3 #7
    ("我看不到這張圖片，無法確認劑量，建議上網搜尋或詢問廠商。", "圖上的字太小看不到，這個一天吃幾顆"),  # #8
    ("我看不到這張圖片，無法確認劑量，建議上網搜尋或詢問廠商。", "照片裡的字太小我看不到，這個一天吃幾顆"),
    ("建議自己上網搜尋。", "你在哪找到這間店的？"),
    (NO_FEATURE, "你幫我提醒對方明天帶傘"),  # 提醒 alone does not ask about features
])
def test_a_word_in_passing_is_not_asking_for_it(text, asked):
    assert reply_policy.strip_missing_data(text, asked, addressed=False) == ""


AS_IS = None  # sent as it is, what it could not see still said


@pytest.mark.parametrize("text, kept", [
    # a picture it cannot see with care or fraud in the reply: sent as before (review 3 #1, (c))
    ("我看不到圖片。如果誤食，請立刻送急診。", AS_IS),
    ("這張照片我這邊沒收到。\n如果孩子誤吞，又覺得頭暈，請趕快就醫。", AS_IS),
    ("我看不到圖片。噎到快叫119！", AS_IS),
    ("我看不到你傳的照片。如果吃了覺得不舒服，建議諮詢藥師。", AS_IS),
    ("我看不到你傳的截圖。如果對方要你去 ATM 操作，那就是詐騙，請立刻撥 165 反詐騙專線。", AS_IS),
    ("我看不到你傳的照片。如果孩子誤吞電池，請立即聯絡 119。", AS_IS),  # review 3 #2
    ("我看不到你傳的截圖。如果對方要你去 ATM 操作，馬上聯絡 110。", AS_IS),
    # §5: whether a lone referral like this is empty talk is Andrew's call; until then, as before
    ("我看不到這張圖片，無法確認劑量資訊。\n一般這類保健食品一天一顆，飯後吃。\n如有不適請立即就醫。", AS_IS),
    # never only the line with the keyword: no premise, the guess reads as if it were seen
    ("我看不到你傳的截圖。這不是詐騙，可以放心點開連結。", AS_IS),
    # where to go, whom to call, what not to hand over (review 4 #3)
    ("我看不到你傳的截圖。如果對方要你轉帳，快叫 110。", AS_IS),
    ("我看不到你傳的照片。這種紅疹建議盡快去看皮膚科。", AS_IS),
    ("我看不到你傳的截圖。對方要你提供驗證碼的話，千萬不要給。", AS_IS),
    # further down, it is the premise of what follows (review 4 #2)
    ("這類保健食品要看成分。\n我沒看到那張照片。\n一般一天一顆，飯後吃。", AS_IS),
    # sent as before, without the no-data sentence (review 4 #4)
    ("我看不到你傳的照片。我沒有這款藥的資料。如果誤食請立刻送急診。", "我看不到你傳的照片。如果誤食請立刻送急診。"),
    # a street number and a joke are not about care
    ("我看不到照片。那家店在中正路 119 號。", ""),
    ("我看不到這張圖片。大家最近都追劇中毒了吧哈哈。", ""),
    # no data: nothing, as on 11/06 (review 3 #3: a fixed urgent line misfired on 服法 and drama)
    ("我沒有本次附上的資料。誤食的話請馬上打 119。", ""),
    ("我沒有本次附上的資料。如果誤食，先催吐再送急診。", ""),
    ("我沒有這款藥的資料。這顆要整顆吞下，不要咬碎；如有不適請就醫。", ""),
    ("我查不到這部電影的資料。男主角叫救護車那段很感人。", ""),
    ("我沒有本次附上的資料，無法確認現價。\n這類藥一天吃兩次。\n如果出現過敏症狀請立即就醫。", ""),
])
def test_a_reply_that_opens_with_what_it_cannot_do(text, kept):
    assert reply_policy.strip_missing_data(text, addressed=False) == (text if kept is AS_IS else kept)


def test_the_reply_pipeline_passes_who_was_asked(monkeypatch):
    # The guards' own fixed lines skip this check at the caller: test_research_verify_claims.py keeps that.
    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE", "0")
    text = "咪寶目前沒有幫忙轉達的功能。"
    kwargs = dict(source_text="合成", request_text="合成", context=[])
    assert main._enforce_new_value_reply(text, addressed=False, **kwargs) == ""
    assert main._enforce_new_value_reply(text, addressed=True, **kwargs) == text
    shape = "若您想了解「某某膠囊」的功效，建議上網搜尋它的成分和用量，或直接詢問廠商。"
    assert main._enforce_new_value_reply(shape, addressed=False, **kwargs) == ""


@pytest.mark.parametrize("model_reply, sent", [
    (NO_FEATURE, False),
    ("可以先把東西放在門口，出門比較不會忘。", True),  # the control: the flush does reach _reply
])
def test_a_burst_nobody_addressed_does_not_send_it(monkeypatch, model_reply, sent):
    """The 11/10 shape through the real burst flush, whose kwargs say nobody addressed the bot."""
    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE", "0")
    monkeypatch.setattr(main, "_quota_exhausted_until_ts", 0.0)
    with (
        patch("main.memory.check_fact_cache", return_value=None),
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._requires_public_research", return_value=False),
        patch("main._llm_chat", return_value=model_reply) as chat,
        patch("main.memory.append_turn"),
        patch("main._maybe_extract_facts"),
        patch("main._finish_burst_without_reply") as finish,
        patch("main._reply") as mock_reply,
    ):
        main._handle_burst_flush("GRP001", "某某你幫我跟對方說一聲謝謝", "TOKEN912")
    chat.assert_called_once()
    assert mock_reply.called is sent
    assert finish.called is not sent


@pytest.mark.parametrize("text", [
    "直接問媽媽最快，也建議上網搜尋。", "只有媽媽知道，建議直接詢問廠商。",
    "直接問媽媽最快，我看不到你傳的圖片，也建議上網搜尋。",  # review 3 #9
])
def test_what_b3_leaves_behind_is_checked_again(monkeypatch, text):
    """B3 takes the first part out and keeps the rest of the sentence (review 2 #5)."""
    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE", "0")
    assert main._enforce_new_value_reply(
        text, source_text="合成", request_text="合成", context=[], addressed=False) == ""


@pytest.mark.parametrize("text", [
    # never looser than the 11/06 rules: a lead-in, a heading or an acknowledgement does not hide what
    # the reply is written around (review 4 #1, #2, #6)
    "我目前沒有本次附上的即時行情資料，以下是一般性的分析：\n1. 半導體近期波動大\n2. 留意財報公布時間",
    "**我看不到你傳的照片**\n這款一般一天兩顆，飯後吃。",
    "好哦！我看不到你傳的照片。這款一般一天兩顆，飯後吃。",
    "了解了！我看不到圖片。這款一天一顆。",
    "好的呀！我看不到圖片。這款一天一顆，飯後吃。",
    "好的！我沒有本次附上的資料，無法確認現價。這檔今天大跌13%，主因是財報不如預期。",
])
def test_never_looser_than_before(text):
    assert reply_policy.strip_missing_data(text, addressed=False) == ""


@pytest.mark.parametrize("off", ["0", "false", "off"])
def test_the_switch_puts_back_the_rules_as_they_were(monkeypatch, off):
    monkeypatch.setenv("LINE_BOT_HANDBACK_GUARD", off)
    assert reply_policy.strip_missing_data("建議直接詢問廠商。", addressed=False) == "建議直接詢問廠商。"
    assert reply_policy.strip_missing_data("我看不到圖片。", addressed=False) == "我看不到圖片。"
    assert reply_policy.strip_missing_data("我沒有本次附上的資料，無法確認現價。") == ""
    # the opening is the first sentence again, as on 11/06 (review 4 #5)
    assert reply_policy.strip_missing_data(
        "好的！我沒有本次附上的資料，無法確認現價。這檔今天大跌13%，主因是財報不如預期。"
    ) == "好的！這檔今天大跌13%，主因是財報不如預期。"


def test_a_broken_new_rule_leaves_the_old_ones_working(monkeypatch):
    def broken(*_a, **_k):
        raise ValueError("synthetic")

    monkeypatch.setattr(reply_policy, "_hands_back", broken)
    stats = {}
    assert reply_policy.strip_missing_data("我沒有本次附上的資料，無法確認現價。這檔今天大跌。", stats=stats) == ""
    assert reply_policy.strip_missing_data("建議直接詢問廠商。", stats=stats) == "建議直接詢問廠商。"
    assert stats["errors"] >= 1


def test_the_hand_back_scan_stays_linear():
    def check(_):
        for text in (
            "我沒" * 10000, "看不到" * 8000 + "圖片", "，看不到" * 6000, "建議搜尋" * 6000,
            "若您想知道" * 5000, "問廠商" * 8000, "我沒有" * 6000 + "功能", "照片我看不到" * 4000,
            "問和廠商" + "和廠商" * 3000, "收到" * 8000 + "，", "怎麼找" * 4000 + "不到",
        ):
            reply_policy.strip_missing_data(text, addressed=False)

    assert cpu_seconds(check, "degenerate hand-backs") < 2


@pytest.mark.parametrize("text", [
    # whom to call, beyond doctors (review post-4 F1)
    "我看不到你傳的截圖。如果你有輕生的念頭，請撥打 1925 安心專線。",
    "我看不到你傳的截圖。心情很糟的話可以打 1995 生命線聊聊。",
    "我看不到你傳的截圖。如果有家暴狀況，請撥 113 保護專線。",
    "我看不到你傳的照片。如果聞到瓦斯味，先開窗、不要開電器，再到屋外打給瓦斯公司。",
    "我看不到你傳的照片。如果有人沒有呼吸，先做 CPR，旁邊有 AED 就拿來用。",
])
def test_a_line_saying_whom_to_call_sends_it_as_before(text):
    assert reply_policy.strip_missing_data(text, "合成", addressed=False) == text


@pytest.mark.parametrize("text", [
    # measured as 11/06 measured it (review post-4 F2)
    "看不到圖。我沒有本次附上的資料。看牙醫。",
    "看不到圖。我沒有本次附上的資料。快報警。",
])
def test_sent_as_before_is_never_shorter_than_the_old_floor(text):
    assert reply_policy.strip_missing_data(text, addressed=False) == ""


@pytest.mark.parametrize("text, kept", [
    ("我看不到你傳的照片。建議上網搜尋。如果誤食請立刻送急診。", "我看不到你傳的照片。如果誤食請立刻送急診。"),
    ("看不到圖。建議上網搜尋。快報警。", "看不到圖。快報警。"),  # the old floor never counted the search line
    ("這類保健食品要看成分。\n我沒看到那張照片。\n我沒有本次附上的資料。", "這類保健食品要看成分。"),
])
def test_more_shapes_around_a_picture_it_cannot_see(text, kept):
    assert reply_policy.strip_missing_data(text, addressed=False) == kept


def test_stats_say_what_went_and_what_stayed():
    stats = {}
    reply_policy.strip_missing_data("我看不到圖片。如果誤食，請立刻送急診。", addressed=False, stats=stats)
    assert stats == {"whole": 1, "kept_for_safety": 1}
    stats = {}
    reply_policy.strip_missing_data("這款一天一顆。\n建議上網搜尋。", addressed=False, stats=stats)
    assert stats == {"alone": 1}


def test_asked_directly_every_line_counts():
    assert reply_policy.strip_missing_data(
        "建議直接詢問廠商。", "這個要問誰？\n我晚點再看", addressed=True) == "建議直接詢問廠商。"


INVARIANT_CORPUS = [
    "若您想了解「某某膠囊」的功效，建議上網搜尋它的成分和用量，或直接詢問廠商。",
    "我看不到這張圖片，無法確認劑量資訊。\n若想知道效果，建議上網搜尋成分，或直接問廠商。",
    NO_FEATURE,
    "好的！我沒有本次附上的資料，無法確認現價。這檔今天大跌13%，主因是財報不如預期。",
    "我目前沒有本次附上的即時行情資料，以下是一般性的分析：\n1. 半導體近期波動大\n2. 留意財報公布時間",
    "**我看不到你傳的照片**\n這款一般一天兩顆，飯後吃。",
    "看不到圖。我沒有本次附上的資料。看牙醫。",
    "看不到圖。建議上網搜尋。快報警。",
    "我看不到你傳的照片。我沒有這款藥的資料。如果誤食請立刻送急診。",
    "這類保健食品要看成分。\n我沒看到那張照片。\n一般一天一顆，飯後吃。",
    "可以退喔！建議先跟賣家確認。",
    "• 建議上網搜尋\n• 或直接問廠商\n• 這款一天一顆",
    "可以。我沒有這家店的最新資料。不過一般超商都收這種發票，拿去櫃檯就行。",
    "收到囉～\n我看不到你傳的照片。\n這款一般一天兩顆，飯後吃。",
    "本益比低不等於便宜，要看獲利狀態。\n\n我沒有本次附上的報價，無法確認現價。",
    "先決定好跌到哪個價位才減碼。另外這個數字我也無法核實，下單前最好自己查一下實際跌幅。",
    "我看不到你傳的截圖。如果你有輕生的念頭，請撥打 1925 安心專線。",
    "直接問媽媽最快，也建議上網搜尋。",
    "你的卡可能被盜刷了。請致電客服。",
    "今天收盤跌了 13%，主因是財報不如預期。",
]

INVARIANT_CORPUS += [
    "收到！\n我無法核實這個說法。\n如果對方要你去ATM操作，那就是詐騙，請撥165。",
    "好的！我沒有本次附上的資料。請立刻就醫。建議上網搜尋。",
    "請立刻就醫。我沒有本次附上的資料。",
    "快報警！\n我看不到你傳的截圖。",
    "這看起來是詐騙：\n1. 不要點連結\n2. 不要回撥\n3. 請直接聯絡官方客服",
    "可以。\n建議直接詢問廠商。",
]


def _sentences(text):
    return [m.group(0).strip() for m in reply_policy._REPLY_SENTENCE_RE.finditer(text) if m.group(0).strip()]


@pytest.mark.parametrize("addressed", [False, True])
@pytest.mark.parametrize("asked", ["", "合成", "這個一天吃幾顆？"])
@pytest.mark.parametrize("text", INVARIANT_CORPUS)
def test_only_ever_takes_more_out_than_the_1106_rules(monkeypatch, text, asked, addressed):
    """With the switch off it is 11/06 exactly; on, what is sent is those sentences or fewer (review post-4 F8)."""
    monkeypatch.setenv("LINE_BOT_HANDBACK_GUARD", "0")
    before = reply_policy.strip_missing_data(text, asked, addressed=addressed)
    monkeypatch.delenv("LINE_BOT_HANDBACK_GUARD")
    after = reply_policy.strip_missing_data(text, asked, addressed=addressed)
    left = iter(_sentences(before))
    assert all(any(sentence == kept for kept in left) for sentence in _sentences(after))


def test_a_long_laugh_next_to_a_hand_back_stays_linear():
    """A nested repeat once backtracked exponentially here (post reviews 1–3 #1)."""
    def check(_):
        for text in (
            "哈" * 5000 + "好笑。\n建議上網搜尋。", "好喔" * 3000 + "X。\n建議直接問廠商。",
            "嗯" * 5000 + "X。\n我看不到你傳的照片。", "哈囉" * 3000 + "吧。建議上網搜尋。",
        ):
            reply_policy.strip_missing_data(text, addressed=False)

    assert cpu_seconds(check, "long laughs") < 2


AS_SENT = None  # the reply as it came


@pytest.mark.parametrize("text, kept", [
    # never fewer safety lines than 11/06 sent (post reviews 1–3)
    ("收到！\n我無法核實這個說法。\n如果對方要你去ATM操作，那就是詐騙，請撥165。",
     "收到！\n\n如果對方要你去ATM操作，那就是詐騙，請撥165。"),
    ("好的！我沒有本次附上的資料。請立刻就醫。建議上網搜尋。", "好的！請立刻就醫。"),
    ("快報警！\n我看不到你傳的截圖。", "快報警！"),
    ("請立刻就醫。照片我沒收到。", "請立刻就醫。"),
    ("請立刻就醫。我沒有本次附上的資料。", ""),  # 11/06 sent nothing here either
    ("會下雨。我沒收到圖片。", "會下雨。"),
    ("可以。\n建議直接詢問廠商。", "可以。"),
    ("有喔！建議直接問店家。", "有喔！"),
    ("我看不到你傳的截圖。千萬不要匯錢給對方，也不要把密碼給任何人。", AS_SENT),
    ("你的信用卡可能被盜刷了。\n不用太緊張。\n先打給銀行的客服。", AS_SENT),
    ("這看起來是詐騙：\n1. 不要點連結\n2. 不要回撥\n3. 請直接聯絡官方客服", AS_SENT),
    ("這款嬰兒推車已經公告召回。\n建議直接聯絡原廠。", AS_SENT),
    ("我無法確認這款有沒有含咖啡因不過一般這類都不含，建議直接問廠商。", AS_SENT),
    # a plant family is not a clinic: the 11/12 shape still goes (post review 1 N2)
    ("我看不到你傳的照片。這款是菊科植物萃取的，一般一天兩顆。\n建議上網搜尋成分，或直接問廠商。", ""),
])
def test_never_fewer_safety_lines_than_before(text, kept):
    assert reply_policy.strip_missing_data(text, "合成", addressed=False) == (text if kept is AS_SENT else kept)


def test_a_sentence_with_contact_details_is_left_for_the_contact_guard(monkeypatch):
    monkeypatch.setattr(reply_policy, "unbacked_contact_details", lambda *a, **k: ["synthetic"])
    assert reply_policy.strip_missing_data("建議直接詢問廠商。", addressed=False) == "建議直接詢問廠商。"


def test_asked_about_a_warranty_the_seller_is_the_answer():
    assert reply_policy.strip_missing_data("請聯絡原廠客服。", "保固怎麼申請？", addressed=False) == "請聯絡原廠客服。"
