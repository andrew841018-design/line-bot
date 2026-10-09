"""2026-10-03 Andrew：bot 被家人質疑「講話沒有根據事實」，要求改正。

Claude（主要供應者）跑在沒有任何工具的 CLI 上，卻憑比事件還舊的訓練記憶否認一則
近期、廣泛報導的公開事件，還說「再查一次」「查不到…紀錄」；同晚另有憑空引用「銀行的
說明」、把假設金額當成股票代號、把有明寫年份的過去新聞日期建成明年的行事曆。
All fixtures are synthetic or public-news phrasing; no real chat content.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

import calendar_extractor  # noqa: E402
import claude_client  # noqa: E402
import gemini_client  # noqa: E402
import main  # noqa: E402
import public_research  # noqa: E402
import reply_policy  # noqa: E402
import reply_provenance  # noqa: E402
import stock_quote  # noqa: E402

TW = ZoneInfo("Asia/Taipei")
SHARED = "兩國元首上個月在機場見面，規格很高，令人感慨。"
CLAIM_REPLY = "再查一次：兩國元首去年在多邊會議見過面。\n\n雙方並沒有破冰到那種規格。"


@pytest.fixture(autouse=True)
def _fresh_provenance():
    reply_provenance.reset()
    yield
    reply_provenance.reset()


def _claims(reply: str, *, searched: bool = False, has_material: bool = False) -> bool:
    return reply_policy.has_unbacked_search_claim(reply, searched=searched, has_material=has_material)


# ── 1. a reply built on a search that never happened is not sent ─────────────

@pytest.mark.parametrize("reply", [
    # the shapes of tonight's replies, de-identified; the whole reply is untrustworthy
    "這個時間點也對不上，查不到總統在機場迎接來訪元首的紀錄。",
    "再查一次：兩國元首去年在多邊會議見過面。\n\n雙方並沒有破冰到那種規格。",
    "這是宣傳帳號發的影片，沒有查到任何獨立新聞報導。\n\n雙方近期仍在打關稅戰。",
    "查到了：元首並未到機場迎接訪客。",
    "我查到兩國元首上個月見過面。",
    "我已經查過官網，活動到月底。",
    "我查了一下，沒有這項優惠。",
    "可以確定我查到了官方公告。",
    "我查了一下行事曆，那天下午有空。",
    "找不到相關報導，這個消息可能是假的。",
    "目前查到的資料都沒有這個說法。",
    "搜尋結果顯示這個優惠只到上個月。",
    "根據某銀行的說明，只有指定通路才有最高回饋。",
    "你說的機場迎接目前查不到相關報導。",
    "妳傳的影片查不到任何獨立新聞報導。",
    "我上網查了，沒有這件事。",
    "我幫你查過了，活動到月底。",
    "我剛查過官網，活動到月底。",
    "我查證過，這個消息是假的。",
    "依據某銀行官方公告，優惠已經取消。",
    "查不到就是沒有這回事。",
    "我找到了相關報導，活動已經延期。",
    "我幫大家查過官網，活動到月底。",
    "我已查證過，這個消息是假的。",
    "我找到一篇報導，活動到月底。",
    "我查過時發現活動已取消。",
    "請放心：查不到任何相關報導，這消息是假的。",
    "據某銀行最新公告，優惠已經取消。",
    "我查到了若干新聞，活動已取消。",
    "查不到，所以應該是假消息。",
    "主要是查不到相關報導，所以不能確定。",
    "我查到他說的話是假的。",
    "結果查到了一篇報導說活動延期。",
    "查到了！明天下午兩點的高鐵有位子。",
    # self-review, round 3
    "幫你查了，高鐵還有位子。",
    "查證後發現這是假消息。",
    "經查，該活動已取消。",
    "搜了一下，沒有這種說法。",
    "Google 了一下，這是假的。",
    "查詢結果：這個優惠已經結束。",
    "我這邊查到的是十月底截止。",
    "我看了一下新聞，沒有這件事。",
    "我查不到你說的那個網站。",
    "目前查不到這個說法。",
    "我找了一下，沒有這家店的資料。",
    "剛剛查了，明天會下雨。",
    "幫大家查過了，週三公休。",
    "上網查了一下，這個說法沒有根據。",
    # round-3 review
    "我查不到總統去機場這件事，應該是謠言。",
    "查不到這件事，應該是謠言。",
    "我沒有查到這件事，應該是假的。",
    "我昨天查過了，活動到月底。",
    "相關報導都查不到，應該是假的。",
    "目前沒看到相關報導，可能是假消息。",
    "幫你查了一下最新消息，目前沒有這個活動。",
    "剛剛查了官網，活動到月底。",
    "查過官網了，活動到月底。",
    "放心：查過了，沒有這回事。",
    "咪寶查了一下，這個優惠已經結束。",
    "我們查了一下，活動已經結束。",
    "根據衛福部公布的資料，這個說法不對。",
    "依據財政部的最新規定，今年不能扣。",
    "據悉，該活動已取消。",
    "我有查，沒有這回事。",
    "查不到相關報導，就代表是假的。",
    "我查不到，應該是假的。",
    "我沒查到，應該是假的。",
    "目前沒有看到任何相關報導，這個消息可能是假的，先不要轉傳。",
    "主流媒體都沒報，應該是假新聞。",
    "各大新聞網站都沒有這則消息。",
    "我有去查，沒有這回事。",
    "我確認過官網了，活動到月底。",
    "我看過官方公告了，活動延期。",
])
def test_unbacked_search_claims_are_caught(reply):
    assert _claims(reply)


@pytest.mark.parametrize("reply", [
    "查不到的話就打電話問銀行。",
    "查不到嗎？換個關鍵字試試。",
    "你再查一次就好。",
    "根據你的說明，明天先帶健保卡。",
    "如果在網站上查不到，明天打給銀行問。",
    "你說「查不到」是指哪個網站？",
    "你可以查查看官網的活動頁。",
    "找不到停車位的話，可以停附近的公有停車場。",
    "要查到最新的優惠，可以看銀行官網的活動頁。",
    "建議先查詢當月優惠細節或打電話預約確認。",
    "老貓食慾變差要留意腎臟和甲狀腺。",
    "查詢結果會顯示罰單金額。",
    "檢方查無不法。",
    "警方查到了詐騙集團的帳戶。",
    "查無此人。",
    "你查到了嗎？",
    "消防局調查到的結果是電線走火。",
    "我找到一個省瓦斯的方法：水滾後蓋上鍋蓋。",
    "記得吃藥前再查一次劑量。",
    "查不到就打電話問銀行。",
    "爸爸查到了公車時刻，搭下一班就好。",
    "你看到「查不到。請重新輸入」時可以換關鍵字。",
    "根據包裝上的說明，加熱三分鐘就可以吃。",
    "媽媽剛剛查到了公車時刻，搭下一班就好。",
    "可以到車站網站再查一次發車時間。",
    "查不到資料就先打電話問。",
    "我找到資料夾了。",
    "依照包裝底部的說明，加熱三分鐘。",
    "爸爸上網查到了公車時刻，搭下一班就好。",
    "查不到公車時刻就打電話問客服。",
    "可以到車站網站，再查一次發車時間。",
    "網路上查得到很多做法。",
    "媽媽剛剛查到了資料，等等傳給大家。",
    "可以查到的資料很多，慢慢看。",
    "郵件查無此地址。",
    "等一下再查一次就好。",
    # each of these does match a claim pattern; the condition, question or quote exempts it
    "如果查不到相關報導，就先別轉傳。",
    "如果我查到相關資料再告訴大家。",
    "真的查不到相關報導嗎？",
    "你看到「查不到相關資料。請重新輸入」時可以換關鍵字。",
    "建議查證後再轉傳。",
    "我也看過這篇文章，寫得很好。",
    "要查了，等一下。",
    "可以查到的活動很多。",
    "看到這個新聞覺得很誇張。",
    "爸爸說他查到了相關報導。",
    # round-3 review
    "網路上查到的資料不一定可靠，吃藥還是要問醫生。",
    "用健保快易通 App 就能查到相關紀錄。",
    "可以在健保署官網查到相關規定。",
    "你們查到的資料沒錯。",
    "你剛剛查到的資料沒錯。",
    "小芳查到的資料沒錯。",
    "姊夫查到的資料沒錯。",
    "爸爸昨天查到的資料沒錯。",
    "找不到優惠券就算了。",
    "回診複查後發現指數正常了，藥還是要照吃。",
    "衛生局抽查後發現三家餐廳不合格，記得避開。",
    "刷卡記得留收據，退款以銀行的說明為準。",
    "這家銀行的據點很多，可以上官網找最近的分行。",
    "依照官網的步驟申請就可以了。",
    "查不到相關資料，就先打電話問銀行。",
    "找不到官網公告，就直接問櫃台。",
    "小美查到的新聞說活動延期了。",
    "語音內容：「我剛剛查了一下，下週三的高鐵還有位子。」這個時間可以。",
    "你剛剛查不到相關資料，可以換關鍵字。",
    "媽媽剛剛查不到相關資料，要再試試。",
    "查不到公車時刻很正常，可以打客服。",
    "店員說查不到你的訂單紀錄，要再打一次客服。",
    "App裡查不到看診紀錄很正常，通常要等三天。",
    "小美查到的新聞是去年的，不用擔心。",
])
def test_conditions_questions_and_advice_are_not_claims(reply):
    assert not _claims(reply)


def test_a_real_search_or_read_page_may_be_cited():
    assert not _claims("目前查到的資料顯示，元首親自到機場迎接。", searched=True)
    assert not _claims("根據這篇報導，元首親自到機場迎接。", has_material=True)
    assert _claims("根據這篇報導，元首親自到機場迎接。", has_material=False)
    # one readable shared page does not show the bank's official page was read
    assert _claims("根據某銀行的官方說明，只有指定通路有回饋。", has_material=True)
    assert not _claims("根據某銀行的官方說明，只有指定通路有回饋。", searched=True)
    # an attached notice may be cited as what it is; a page that was read, as that page
    assert not reply_policy.has_unbacked_search_claim(
        "根據健保署的公告，11月起掛號費調整。", searched=False, has_material=True, official_ok=True)
    assert _claims("根據健保署的公告，11月起掛號費調整。", has_material=True)
    assert not _claims("根據官網說明，要先登錄才有回饋。", has_material=True)
    assert _claims("根據官網說明，要先登錄才有回饋。", has_material=False)
    # reading counts only when something was attached to read
    assert not _claims("我看了一下文章，重點是活動延期。", has_material=True)
    assert _claims("我看了一下文章，重點是活動延期。", has_material=False)


def test_the_reply_check_drops_the_whole_reply(monkeypatch):
    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE", "0")
    reply = "再查一次：兩國元首去年在多邊會議見過面。\n\n雙方並沒有破冰到那種規格。"
    assert main._enforce_new_value_reply(
        reply, source_text=SHARED, request_text=SHARED, context=[], addressed=False,
    ) == ""
    assert main._enforce_new_value_reply(
        reply, source_text=SHARED, request_text=SHARED, context=[], addressed=False,
        searched=True,
    ) == reply


# ── 2. who searched ──────────────────────────────────────────────────────────

def test_each_generation_starts_unsearched(monkeypatch):
    reply_provenance.mark_searched()
    monkeypatch.setattr("claude_client.chat", lambda *a, **k: "回覆")
    assert main._llm_chat("問題", [], [], None) == "回覆"
    assert reply_provenance.searched() is False


def test_the_local_model_never_counts_as_searched(monkeypatch):
    import local_llm

    monkeypatch.setattr(local_llm, "chat", lambda *a, **k: "本機模型的合成回覆內容")
    reply_provenance.mark_searched()
    assert main._local_text_llm_fallback("問題", context=[]) == "本機模型的合成回覆內容"
    assert reply_provenance.searched() is False


def test_lite_marks_a_search_only_when_its_answer_is_used(monkeypatch):
    import lite_reply
    import local_llm

    monkeypatch.setattr(local_llm, "runtime_enabled", lambda: True)
    monkeypatch.setattr(lite_reply, "_needs_lite_opinion_context", lambda *a, **k: True)
    monkeypatch.setattr(lite_reply, "_opinion_context_contains_material", lambda *a, **k: False)
    monkeypatch.setattr(lite_reply, "_collect_opinion_reference_context", lambda *a, **k: "合成證據")
    monkeypatch.setattr(local_llm, "chat", lambda *a, **k: None)
    reply_provenance.reset()
    assert lite_reply._try_local_llm("這個政策是真的嗎", context=[]) is None
    assert reply_provenance.searched() is False
    monkeypatch.setattr(local_llm, "chat", lambda *a, **k: "合成的有根據回答，政策下個月上路。")
    assert lite_reply._try_local_llm("這個政策是真的嗎", context=[])
    assert reply_provenance.searched() is True
    # an answer from material already in the chat, with no lookup, is not a search
    reply_provenance.reset()
    monkeypatch.setattr(lite_reply, "_opinion_context_contains_material", lambda *a, **k: True)
    assert lite_reply._try_local_llm("這個政策是真的嗎", context=[])
    assert reply_provenance.searched() is False


def test_gemini_grounding_counts_as_a_search(monkeypatch):
    def gemini_with_grounding(*_a, **_k):
        reply_provenance.mark_searched()
        return "回覆"

    monkeypatch.setattr("claude_client.chat", lambda *a, **k: None)
    monkeypatch.setattr(main.gemini_client, "chat", gemini_with_grounding)
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    assert main._llm_chat("問題", [], [], None) == "回覆"
    assert reply_provenance.searched() is True


@pytest.fixture
def research_env(monkeypatch):
    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE", "0")
    monkeypatch.setattr(main, "_thinking_indicator", lambda *_: nullcontext())
    monkeypatch.setattr(main.memory, "get_context", lambda *_: [])
    monkeypatch.setattr(main.memory, "top_facts", lambda *a, **k: [])
    monkeypatch.setattr(main, "_get_persona_notes", lambda *_: [])
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_: False)
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_finish_research_without_reply", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(main, "_reply", lambda _tok, text, **_kw: sent.append(text))
    event = SimpleNamespace(source=SimpleNamespace(user_id="U_TEST"), reply_token="T_GR", message=None)
    return SimpleNamespace(event=event, sent=sent)


def test_the_research_path_may_say_what_it_found(monkeypatch, research_env):
    rows = [{"url": "https://example.com/a",
             "full_text": "合成資料：總統九月下旬親自到機場迎接來訪元首，為數十年來首次。"}]
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _text: rows)
    answer = "目前查到的資料顯示，這是數十年來美國總統第一次親自到機場迎接外國元首。"
    prompts = []
    monkeypatch.setattr(main, "_llm_chat", lambda prompt, *_a: prompts.append(prompt) or answer)
    claim = f"{datetime.now(TW).year}年9月24日總統在華府附近的機場歡迎來訪元首"
    assert main._handle_web_research_question(research_env.event, "G_GR", claim, addressed=False)
    assert research_env.sent == [answer]
    assert "已由程式實際搜尋" in prompts[0]


def test_the_research_path_checks_its_own_answers(monkeypatch, research_env):
    # through the real _llm_chat: the search rows in the prompt back the answer
    rows = [{"url": "https://example.com/a",
             "full_text": "合成資料：總統九月下旬親自到機場迎接來訪元首，為數十年來首次。"}]
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _text: rows)
    answer = "目前查到的資料顯示，這是數十年來美國總統第一次親自到機場迎接外國元首。"
    monkeypatch.setattr("claude_client.chat", lambda *a, **k: answer)
    claim = f"{datetime.now(TW).year}年9月24日總統在華府附近的機場歡迎來訪元首"
    assert main._handle_web_research_question(research_env.event, "G_GR", claim, addressed=False)
    assert research_env.sent == [answer]


def test_the_answering_models_own_search_counts(monkeypatch, research_env):
    page = "https://example.com/page"
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _text: [])
    monkeypatch.setattr(
        main, "_prefetch_urls",
        lambda text: "（以下是連結的內容）\n合成正文：活動辦到月底，限會員參加。\n\n" + text,
    )
    answer = "目前查到的資料顯示活動已延長到下個月。"

    def grounded(_prompt, *_a):
        reply_provenance.mark_searched()  # Gemini searched while answering
        return answer

    monkeypatch.setattr(main, "_llm_chat", grounded)
    assert main._handle_web_research_question(research_env.event, "G_GR", f"這個活動是真的嗎 {page}")
    assert research_env.sent == [answer]


def test_a_read_link_alone_is_not_a_search(monkeypatch, research_env):
    page = "https://example.com/page"
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _text: [])
    monkeypatch.setattr(
        main, "_prefetch_urls",
        lambda text: "（以下是連結的內容）\n合成正文：活動辦到月底，限會員參加。\n\n" + text,
    )
    prompts = []
    def ungrounded(prompt, *_a):
        reply_provenance.reset()
        prompts.append(prompt)
        return "目前查到的資料顯示活動辦到月底。"

    monkeypatch.setattr(main, "_llm_chat", ungrounded)
    assert main._handle_web_research_question(research_env.event, "G_GR", f"這個活動是真的嗎 {page}")
    assert prompts and "已由程式實際搜尋" not in prompts[0]
    assert research_env.sent == []  # it claims a search on a page it only read


# ── 3. what each provider is told ────────────────────────────────────────────

# a shared link also adds the fact-check rules
SEARCH_INPUT = "這篇 https://example.com/a 說的是真的嗎"
SEARCH_INSTRUCTIONS = (
    "這個我查了一下", "這個我查了但查不到耶", "用 Google 搜尋查一下再回答", "立刻用 Google 搜尋",
    "你自己去搜就對了", "先用 Google 搜尋驗證", "直接在內部用 Google 搜尋",
    "必須先 Google 搜尋影片標題", "我搜尋的整理文是", "Google 搜尋最新背景再答",
    "搜尋可靠來源核實", "查證仍須做",
)


def test_claude_is_told_it_cannot_search():
    system, _user = claude_client._build_cli_prompt(SEARCH_INPUT, [], [], None)
    assert reply_policy.NO_SEARCH_CONTRACT in system
    payload = claude_client._build_payload(SEARCH_INPUT, [], [], None)
    assert reply_policy.NO_SEARCH_CONTRACT in payload["system"]
    # nothing earlier in the prompt still teaches it to search or to say it did
    for instruction in SEARCH_INSTRUCTIONS:
        assert instruction not in system
        assert instruction not in payload["system"]


def test_the_local_fallback_is_told_it_cannot_search():
    assert reply_policy.NO_SEARCH_CONTRACT in main._local_text_fallback_system_prompt()


def test_gemini_keeps_its_search_tool_and_gets_the_shared_rule():
    system = gemini_client._build_system_instruction([], None, user_input=SEARCH_INPUT)
    assert reply_policy.NO_SEARCH_CONTRACT not in system
    assert "你的記憶可能過時" in system
    for instruction in SEARCH_INSTRUCTIONS:
        assert instruction in system


def test_burst_instruction_asks_for_grounded_corrections():
    assert "近期公開事件" in main._BURST_REPLY_INSTRUCTION
    assert "沒有附資料就不要糾正" in main._BURST_REPLY_INSTRUCTION


# ── 4. dated public-affairs claims get a real search ─────────────────────────

@pytest.mark.parametrize("text", [
    "2026年9月24日美國總統在華府附近的機場歡迎來訪元首",
    "最近兩國元首在白宮會面，還有國宴",
    "今年的峰會兩國元首簽了貿易協議",
])
def test_dated_public_affairs_claims_need_research(text):
    assert public_research.requires_current_research(text)


@pytest.mark.parametrize("text", [
    "今天小明去總統府參觀",
    "最近小美在美國離婚了",
    "最近表哥在外交部上班，今天辭職了",
    "今天阿嬤去立法院陳情，下午跟立委會面",
    "今天帶小孩去總統府參觀，導覽員很熱情歡迎大家",
    "最近美國好多人去旅行",
    "2026年9月24日我們去機場接阿姨",
    "歡迎大家週末來家裡吃飯",
    "總統很辛苦",
    "最近總統瘦了好多",
    "今天小明去機場迎接總統",
    "最近總統很忙，週末社區要歡迎新住戶",
    "最近元首題材的老電影片尾很感人週末社區要歡迎新住戶",
    "今天陳小明與總統會面",
    "最近看了一部元首題材的老電影，片尾很感人，週末社區要歡迎新住戶",
    "今天歡迎來吃飯",
    "最近接待親戚好累",
    "今天小明當選班級主席",
    "最近日本好冷喔",
    "今天去日本料理吃飯",
])
def test_private_undated_or_everyday_text_does_not(text):
    assert not public_research.requires_current_research(text)


@pytest.mark.parametrize("text", [
    "2026年9月24日我們去機場接阿姨",
    "最近表哥在外交部上班，今天辭職了",
    "今天阿嬤去立法院陳情，下午跟立委會面",
    "今天帶小孩去總統府參觀，導覽員很熱情歡迎大家",
    "最近媽媽說總統在機場歡迎來訪元首",
    "今天大嫂去迎接總統，下午回診做化療",
    "今天總統宣布停火，下午舅舅要住院",
])
def test_family_text_never_becomes_a_search_query(text):
    assert public_research.public_query(text) == ""
    assert not public_research.requires_current_research(text)


# ── 5. a price is not a ticker ───────────────────────────────────────────────

@pytest.mark.parametrize(("text", "symbols"), [
    ("假設台積電有3000，那就賺很多了", ["2330.TW"]),
    ("台積電漲到3000", ["2330.TW"]),
    ("台積電3000元", ["2330.TW"]),
    ("台積電3000", ["2330.TW"]),
    ("台積電有 3000 元", ["2330.TW"]),
    ("台積電1,050元", ["2330.TW"]),
    ("我買了3000股台積電", ["2330.TW"]),
    ("目標價3000", []),
    ("2330多少", ["2330.TW"]),
    ("2330股價多少", ["2330.TW"]),
    ("我有2330，現在多少", ["2330.TW"]),
    ("3008現在多少", ["3008.TW"]),
    ("2330,2317,2454多少", ["2330.TW", "2317.TW", "2454.TW"]),
    ("2330股息多少", ["2330.TW"]),
    ("買了3000股本來打算長期持有", []),
    ("2330股本多少", ["2330.TW"]),
    ("買了3000股本週再加碼", []),
    ("先賣3000股本金拿回來", []),
    ("3000股本身不多", []),
    ("持有5000股份", []),
    ("0050現在多少", ["0050.TW"]),
])
def test_a_price_or_quantity_is_not_a_ticker(text, symbols):
    assert stock_quote.detect_symbols(text) == symbols


def test_a_market_word_does_not_turn_a_price_into_a_ticker():
    assert "3000.TW" not in stock_quote.detect_symbols("台股台積電有3000元")
    assert "2330.TW" in stock_quote.detect_symbols("台股2330多少")


# ── 6. an explicit past year is not next year's event ────────────────────────

NO_EVENT = {
    "has_event": False, "is_cancellation": False, "title": None, "date": None,
    "time": None, "location": None, "participants": [], "cancel_target_keyword": None,
    "event_type": "family_gathering",
}


def _events(text: str) -> list[tuple[str, str | None]]:
    return [(e["date"], e.get("time")) for e in calendar_extractor.extract_many(text, primary=NO_EVENT)["events"]]


def test_a_past_dated_news_statement_is_not_a_family_event():
    year = datetime.now(TW).year - 1
    assert _events(f"你這個太舊了吧{year}年9月24日總統夫婦在華府附近的機場，歡迎來訪元首夫婦") == []
    assert _events(f"{year}年9月24日 18:00 全家聚餐") == []  # privacy-safe-fixture


def test_an_explicit_future_year_is_kept():
    year = datetime.now(TW).year + 1
    assert _events(f"{year}年1月5日要回診") == [(f"{year}-01-05", None)]
    assert _events(f"{year}年9月24日 18:00 全家聚餐") == [(f"{year}-09-24", "18:00")]  # privacy-safe-fixture


def test_a_republic_of_china_year_is_an_explicit_year():
    roc = datetime.now(TW).year - 1911
    assert _events(f"{roc - 1}年9月24日 18:00 全家聚餐") == []  # privacy-safe-fixture
    assert _events(f"{roc + 1}年1月5日要回診") == [(f"{roc + 1 + 1911}-01-05", None)]


def test_a_yearless_past_date_still_means_next_year():
    today = datetime.now(TW).date()
    assert _events("1月1日 18:00 全家聚餐") == [(f"{today.year + 1}-01-01", "18:00")]  # privacy-safe-fixture


# ── 7. end to end (reviewer 4): the real handlers, threads and Gemini ────────

def _burst(monkeypatch, claude_reply, prefetch=lambda t: t, cached=None):
    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE", "0")
    monkeypatch.setattr("claude_client.chat", lambda *a, **k: claude_reply)
    monkeypatch.setattr(main.memory, "get_context", lambda *_a: [])
    monkeypatch.setattr(main.memory, "check_fact_cache", lambda *_a: cached)
    monkeypatch.setattr(main.memory, "top_facts", lambda *a, **k: [])
    monkeypatch.setattr(main.memory, "store_fact_cache", lambda *a, **k: None)
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_get_persona_notes", lambda *_a: [])
    monkeypatch.setattr(main, "_requires_public_research", lambda *_a: False)
    monkeypatch.setattr(main, "_prefetch_urls", prefetch)
    monkeypatch.setattr(main, "_thinking_indicator", lambda *_: nullcontext())
    monkeypatch.setattr(main, "_register_inbound_reply_batch", lambda *a, **k: None)
    monkeypatch.setattr(main, "_start_burst_finance_extraction", lambda *a, **k: None)
    monkeypatch.setattr(main, "_maybe_extract_facts", lambda *a, **k: None)
    monkeypatch.setattr(main, "_maybe_capture_calendar_event", lambda *a, **k: None)
    sent, silent = [], []
    monkeypatch.setattr(main, "_reply", lambda _tok, text, **_kw: sent.append(text))
    monkeypatch.setattr(main, "_finish_burst_without_reply", lambda *a, **k: silent.append(True))
    reply_provenance.mark_searched()  # stale in the calling thread; the burst runs in its own
    worker = threading.Thread(target=main._handle_burst_flush, args=("G_B", SHARED, "T_B", ["M_B"]))
    worker.start()
    worker.join(20)
    assert not worker.is_alive()
    return sent, silent


def test_a_burst_reply_claiming_a_search_is_not_sent(monkeypatch):
    sent, silent = _burst(monkeypatch, CLAIM_REPLY)
    assert sent == [] and silent == [True]


def test_an_ordinary_burst_reply_is_still_sent(monkeypatch):
    advice = "想確認細節，可以等官方公布的行程表再安排。"
    sent, silent = _burst(monkeypatch, advice)
    assert sent == [advice] and silent == []


def _read_page(text):
    main._note_link_content()  # real text came back from the shared link
    return "（以下是連結的內容）\n合成正文：活動辦到月底，限會員參加。\n\n" + text


def test_a_burst_may_cite_only_a_page_it_actually_read(monkeypatch):
    cited = "根據這篇報導，會員限定的活動辦到月底。"
    assert _burst(monkeypatch, cited, prefetch=_read_page) == ([cited], [])
    assert _burst(monkeypatch, cited) == ([], [True])


def test_an_explicit_reply_claiming_a_search_is_not_sent(monkeypatch):
    from linebot.v3.webhooks import MessageEvent, TextMessageContent

    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE", "0")
    monkeypatch.setattr("claude_client.chat",
                        lambda *a, **k: "這個時間點也對不上，查不到總統在機場迎接來訪元首的紀錄。")
    evt = MagicMock(spec=MessageEvent)
    evt.message = TextMessageContent(id="MSG_GR", text="咪寶 兩國元首真的見面了嗎", quoteToken="qt")
    evt.source = SimpleNamespace(type="group", group_id="G_E", user_id="U_TEST")
    evt.reply_token = "T_E"
    reply_provenance.mark_searched()  # a stale value from an earlier task on this worker
    with (
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._build_quoted_block", return_value=""),
        patch("main._requires_public_research", return_value=False),
        patch("main._get_explicit_market_quote_reply", return_value=None),
        patch("main._prefetch_urls", side_effect=lambda t: t),
        patch("main._thinking_indicator", side_effect=lambda *_a, **_k: nullcontext()),
        patch("main.memory.append_turn"),
        patch("main._append_bot_turn"),
        patch("main._maybe_extract_facts"),
        patch("main._try_save_correction"),
        patch("main._maybe_capture_calendar_event"),
        patch("main._finish_explicit_without_reply") as silent,
        patch("main._reply") as mock_reply,
    ):
        main._handle_explicit_text(evt, "G_E", "兩國元首真的見面了嗎")
    assert mock_reply.call_count == 0
    assert silent.call_count == 1


def _fake_gemini(monkeypatch, *, grounded: bool):
    web = SimpleNamespace(uri="https://example.com/news", title="合成新聞")
    meta = SimpleNamespace(grounding_chunks=[SimpleNamespace(web=web)]) if grounded else None
    response = SimpleNamespace(
        text="合成資料顯示九月下旬有一場元首會談，雙方簽了貿易協議。",
        candidates=[SimpleNamespace(grounding_metadata=meta, finish_reason=SimpleNamespace(name="STOP"),
                                    content=SimpleNamespace(parts=[]))],
        usage_metadata=None,
    )
    session = SimpleNamespace(send_message=lambda *_a, **_k: response)
    monkeypatch.setattr(gemini_client, "_client", SimpleNamespace(chats=SimpleNamespace(create=lambda **_k: session)))
    monkeypatch.setattr(gemini_client, "_track_usage", lambda *_a: None)
    return gemini_client._run(
        "gemini-test", user_input="最近有什麼國際新聞", context=[], facts=[],
        persona_notes=None, recall_hits=None, case_hits=None, group_id=None,
    )


def test_real_gemini_grounding_marks_a_search(monkeypatch):
    assert _fake_gemini(monkeypatch, grounded=True)
    assert reply_provenance.searched() is True


def test_real_gemini_without_grounding_does_not(monkeypatch):
    assert _fake_gemini(monkeypatch, grounded=False)
    assert reply_provenance.searched() is False


# ── 8. post-implementation review (2026-10-04) ───────────────────────────────

def test_a_search_claim_in_a_removed_operation_sentence_still_drops_the_reply(monkeypatch):
    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE", "0")
    reply = "我查過官網，已幫你把提醒改到週四。報名截止日是週三下午五點。"
    assert main._enforce_new_value_reply(
        reply, source_text="報名", request_text="報名", context=[], addressed=False,
    ) == ""


CLAIM = "我查到了官方公告，活動到月底。"


def test_every_generated_reply_is_checked(monkeypatch):
    monkeypatch.setattr("claude_client.chat", lambda *a, **k: CLAIM)
    assert main._llm_chat("活動到什麼時候", [], [], None) == ""
    # burst, direct questions and research check the reply themselves
    assert main._caller_checked(main._llm_chat, "活動到什麼時候", [], [], None) == CLAIM


def test_a_grounded_gemini_reply_may_say_what_it_found(monkeypatch):
    def grounded(*_a, **_k):
        reply_provenance.mark_searched()
        return CLAIM

    monkeypatch.setattr("claude_client.chat", lambda *a, **k: None)
    monkeypatch.setattr(main.gemini_client, "chat", grounded)
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    assert main._llm_chat("活動到什麼時候", [], [], None) == CLAIM


def test_the_local_model_reply_is_checked(monkeypatch):
    import local_llm

    monkeypatch.setattr(local_llm, "chat", lambda *a, **k: CLAIM)
    assert main._local_text_llm_fallback("活動到什麼時候", context=[]) == ""


def test_an_audio_reply_claiming_a_search_is_not_sent(monkeypatch):
    monkeypatch.setattr("claude_client.chat", lambda *a, **k: None)  # Claude takes no audio
    monkeypatch.setattr(main.gemini_client, "chat", lambda *a, **k: CLAIM)
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_download_content", lambda _id: b"synthetic audio bytes")
    monkeypatch.setattr(main.memory, "get_context", lambda *_a: [])
    monkeypatch.setattr(main.memory, "top_facts", lambda *a, **k: [])
    monkeypatch.setattr(main, "_get_persona_notes", lambda *_a: [])
    monkeypatch.setattr(main, "_thinking_indicator", lambda *_: nullcontext())
    monkeypatch.setattr(main.memory, "log_raw_message_meta", lambda *a, **k: None)
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_maybe_extract_facts", lambda *a, **k: None)
    sent, finished = [], []
    monkeypatch.setattr(main, "_reply", lambda _tok, text, **_kw: sent.append(text))
    monkeypatch.setattr(main, "_mark_inbound_reply_completed_no_reply", lambda token, **_k: finished.append(token))
    event = SimpleNamespace(message=SimpleNamespace(id="AUD1"), reply_token="T_A",
                            source=SimpleNamespace(user_id="U_TEST"))
    main._handle_audio_message(event, "G_A")
    assert sent == []
    assert finished == ["T_A"]  # finished as intentionally silent


def test_an_empty_grounded_rewrite_does_not_vouch_for_the_old_answer(monkeypatch):
    web = SimpleNamespace(uri="https://example.com/n", title="合成")
    empty = SimpleNamespace(
        text="",
        candidates=[SimpleNamespace(grounding_metadata=SimpleNamespace(grounding_chunks=[SimpleNamespace(web=web)]),
                                    finish_reason=SimpleNamespace(name="STOP"), content=SimpleNamespace(parts=[]))],
        usage_metadata=None,
    )
    session = SimpleNamespace(send_message=lambda *_a, **_k: empty)
    monkeypatch.setattr(gemini_client, "_track_usage", lambda *_a: None)
    out = gemini_client._quality_gate(session, "您說得對，這個優惠到月底。", [], "這個優惠到月底嗎", None)
    assert out  # the earlier, unsearched answer comes back
    assert reply_provenance.searched() is False


def test_a_new_gemini_session_does_not_inherit_an_earlier_search(monkeypatch):
    reply_provenance.mark_searched()  # a grounded draft from a session that then failed
    assert _fake_gemini(monkeypatch, grounded=False)
    assert reply_provenance.searched() is False


ALL_PACKS_INPUT = "最近總統大選和關稅新聞 https://www.youtube.com/watch?v=abc 台積電股價漲了，月薪5萬夠嗎，昨晚還有地震"


def test_no_rule_pack_tells_claude_to_search():
    gemini = gemini_client._build_system_instruction([], None, user_input=ALL_PACKS_INPUT)
    for pack in (gemini_client._RULE_POLITICS, gemini_client._RULE_FACTCHECK, gemini_client._RULE_VIDEO_COMMENTARY,
                 gemini_client._RULE_FINANCE, gemini_client._RULE_NUMBERS, gemini_client._RULE_EARTHQUAKE,
                 gemini_client._RULE_NEWS_CASE):
        assert pack in gemini  # the input really loads every rule pack
    system, _user = claude_client._build_cli_prompt(ALL_PACKS_INPUT, [], [], None)
    head = system.split(reply_policy.NO_SEARCH_CONTRACT)[0].replace("Google 搜尋結果是英文", "")
    for phrase in VERIFY_FIRST:
        assert phrase not in head


VERIFY_FIRST = ("Google 搜尋", "主動查", "我可以查", "搜尋查", "上網查", "先搜尋", "搜尋可靠來源",
                "外部查證的主張先查證", "內部查找", "先查證主張", "先核對原始主張", "核實最新事實",
                "查證可核實", "仍需先查證", "先查證實際主張")


def test_the_local_models_are_not_told_to_verify_outside():
    import lite_reply
    import video_reply

    assert "外部查證" not in video_reply.VIDEO_COMMENTARY_CONTRACT_NO_SEARCH
    local = main._local_text_fallback_system_prompt().split(reply_policy.NO_SEARCH_CONTRACT)[0]
    for prompt in (local, lite_reply._LOCAL_LLM_OPINION_SYSTEM_PROMPT):
        for phrase in VERIFY_FIRST:
            assert phrase not in prompt


@pytest.mark.parametrize(("text", "query"), [
    # a public-affairs claim sends only public words: names, illnesses stay home
    ("今天總統宣布停火，下午記得去剪頭髮", "今天 總統 宣布 停火"),
    ("最近兩國元首在白宮會面，還有國宴", "最近 兩國 元首 白宮 會面"),
    ("聽說今天總統宣布停火", "今天 總統 宣布 停火"),
    ("今天賴總統宣布停火", "今天 總統 宣布 停火"),
    ("日本石破首相今天訪美", "日本 首相 今天 訪美"),
    # a nickname doing something keeps the whole message out of the search
    ("今天總統宣布停火，下午小芳要去剪頭髮", ""),
    # a price/policy claim keeps its own clauses as written
    ("今年雞蛋產量過剩，價格會跌嗎", "今年雞蛋產量過剩，價格會跌嗎"),
    ("今天總統宣布關稅，下午記得去看牙醫", "今天總統宣布關稅"),
    ("最近物價上漲 下午記得去剪頭髮", "最近物價上漲"),
])
def test_only_the_public_clause_is_a_search_query(text, query):
    assert public_research.search_query(text) == query


@pytest.mark.parametrize("text", [
    "今天總統宣布停火：小芳確診乳癌",
    "今天總統宣布停火 小芳確診乳癌",
    "今天陳小明與總統會面討論離婚官司",
    "最近小芳跟總統簽了協議",
    "最近疫情又升溫 小芳也確診了",
    "最近疫情又升溫\n小芳也在這波疫情確診了",
    "最近利率上漲，阿明的房貸利率變成2.5%",
    "今天總統宣布停火，小芳說她的股票漲價了",
    "小芳確診後最近可以去旅遊嗎",
    "最近疫情升溫，王先生確診了",
    "最近流感疫情很嚴重小芳也確診了",
    "今天小芳在台大醫院生產了",
])
def test_someone_elses_news_next_to_a_public_one_is_not_searched(text):
    assert not public_research.requires_current_research(text)


def test_only_the_public_clause_reaches_the_search_engines(monkeypatch, research_env):
    captured = []
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda text: captured.append(text) or [])
    monkeypatch.setattr(main, "_llm_chat", lambda *_a: "")
    main._handle_web_research_question(
        research_env.event, "G_GR", "今天總統宣布停火，下午記得去剪頭髮", addressed=False,
    )
    assert captured == ["今天 總統 宣布 停火"]


def test_spaced_or_two_digit_republic_years_are_explicit():
    year = datetime.now(TW).year
    assert _events(f"{year - 1}年 9月24日 18:00 全家聚餐") == []  # privacy-safe-fixture
    assert _events("民國99年9月24日 18:00 全家聚餐") == []  # privacy-safe-fixture
    assert _events("25年9月24日 18:00 全家聚餐") == []  # an unreadable year is not "no year"  # privacy-safe-fixture
    # the year is read, so the rest of the range shares it (not next year)
    assert _events("民國99年9月24日到9月26日全家出遊") == []
    assert _events(f"{year - 1}年 9月24日到9月26日全家出遊") == []


def test_a_list_or_range_shares_its_written_year():
    year = datetime.now(TW).year
    assert _events(f"{year + 2}年1月5日、1月6日要回診") == [(f"{year + 2}-01-05", None), (f"{year + 2}-01-06", None)]
    assert _events(f"{year - 1}年9月24日到9月26日全家出遊") == []
    assert _events(f"{year - 1}年9月24日到9月26日 18:00 全家出遊") == []


# ── 9. post-implementation review, round 2 (2026-10-04) ──────────────────────

REPORT_CLAIM = "根據最新報導，活動已經取消，明天不用過去了。"


def test_a_cited_report_needs_something_attached(monkeypatch):
    monkeypatch.setattr("claude_client.chat", lambda *a, **k: REPORT_CLAIM)
    assert main._llm_chat("晚餐吃什麼", [], [], None) == ""
    file_prompt = "請分析這個檔案的內容並回應。\n\n--- 內容開始 ---\n合成文章內容\n--- 內容結束 ---"
    assert main._llm_chat(file_prompt, [], [], None) == REPORT_CLAIM


def test_an_empty_reply_after_a_dropped_one_finishes_the_message(monkeypatch):
    finished = []
    monkeypatch.setattr(main, "_mark_inbound_reply_completed_no_reply", lambda token, **_k: finished.append(token))
    assert main._reply("T_X", "") is False
    assert finished == []  # nothing was dropped
    reply_provenance.mark_dropped()
    assert main._reply("T_X", "") is False
    assert finished == ["T_X"]


def _pending_env(monkeypatch, reply):
    monkeypatch.setattr("claude_client.chat", lambda *a, **k: reply)
    monkeypatch.setattr(main, "_pending_reply_enabled", lambda: True)
    monkeypatch.setattr(main, "_drop_stale_pending", lambda *_a: None)
    monkeypatch.setattr(main, "_load_pending_explicit",
                        lambda: {"G_P": [{"type": "text", "text": "合成的待回訊息", "message_id": "M_P"}]})
    monkeypatch.setattr(main, "_pending_text_with_quote", lambda item, _g: item["text"])
    monkeypatch.setattr(main.memory, "get_context", lambda *_a: [])
    monkeypatch.setattr(main.memory, "top_facts", lambda *a, **k: [])
    monkeypatch.setattr(main, "_get_persona_notes", lambda *_a: [])
    finished = []
    monkeypatch.setattr(main, "_complete_pending_without_reply", lambda g, ids: finished.append((g, ids)) or True)
    return finished


def test_a_dropped_pending_reply_is_finished_not_retried(monkeypatch):
    finished = _pending_env(monkeypatch, CLAIM)
    assert main._peek_text_pending_for_drain("G_P", 2, 30.0) == []
    assert finished == [("G_P", ["M_P"])]


def test_a_dropped_piggyback_reply_is_finished_not_retried(monkeypatch):
    finished = _pending_env(monkeypatch, CLAIM)
    assert main._peek_pending_for_piggyback("G_P") is None
    assert finished == [("G_P", ["M_P"])]


def test_a_failed_quota_recheck_does_not_vouch_for_lite(monkeypatch):
    import lite_reply

    def grounded_then_failed(*_a, **_k):
        reply_provenance.mark_searched()
        raise RuntimeError("synthetic rewrite failure")

    monkeypatch.setattr("claude_client.chat", lambda *a, **k: None)
    monkeypatch.setattr(main, "_quota_exhausted", lambda: True)
    monkeypatch.setattr(main, "_quota_recheck_allowed", lambda: True)
    monkeypatch.setattr(main, "_record_quota_recheck_attempt", lambda: None)
    monkeypatch.setattr(main.gemini_client, "chat", grounded_then_failed)
    monkeypatch.setattr(lite_reply, "lite_reply", lambda *a, **k: CLAIM)
    assert main._llm_chat("活動到什麼時候", [], [], None) == ""


def test_dates_with_times_in_a_list_or_range_share_the_year():
    year = datetime.now(TW).year
    assert sorted(_events(f"{year + 2}年1月5日 18:00、1月6日 18:00 全家聚餐")) == [  # privacy-safe-fixture
        (f"{year + 2}-01-05", "18:00"), (f"{year + 2}-01-06", "18:00")]
    assert _events(f"{year - 1}年9月24日 18:00 到9月26日 18:00 全家出遊") == []


def test_a_range_past_december_moves_into_the_next_year():
    year = datetime.now(TW).year
    assert _events(f"{year}年12月31日到1月2日 全家出遊") == [(f"{year + 1}-01-02", None)]
    assert sorted(_events(f"{year}年12月31日 18:00、1月1日 18:00 全家聚餐")) == [  # privacy-safe-fixture
        (f"{year}-12-31", "18:00"), (f"{year + 1}-01-01", "18:00")]


def test_a_year_we_cannot_read_makes_no_event_however_it_is_spaced():
    assert _events("25 年 9月24日 18:00 全家聚餐") == []  # privacy-safe-fixture
    assert _events("25年  9月24日 18:00 全家聚餐") == []  # privacy-safe-fixture
    assert _events("25年9月24日到9月26日全家出遊") == []


# ── 10. post-implementation review, round 3 (2026-10-04) ─────────────────────

def test_a_cached_reply_that_needs_a_search_is_answered_afresh(monkeypatch):
    advice = "想確認細節，可以等官方公布的行程表再安排。"
    sent, silent = _burst(monkeypatch, advice, cached=CLAIM_REPLY)
    assert sent == [advice] and silent == []


def test_a_cached_reply_with_an_address_it_cannot_back_is_answered_afresh(monkeypatch):
    # 2026-10-10 review: what backed the address then (a page, older turns) is
    # gone; replaying it would end silent for the cache's 7 days.
    advice = "想確認細節，可以等官方公布的行程表再安排。"
    cached = "接待地點在測試市中正區青島東路3之2號，記得提早到。"
    sent, silent = _burst(monkeypatch, advice, cached=cached)
    assert sent == [advice] and silent == []


def test_lite_lookups_count_as_a_search(monkeypatch):
    import lite_reply

    monkeypatch.setattr(lite_reply, "_extract_wiki_query", lambda _t: "合成詞條")
    monkeypatch.setattr(lite_reply, "_wiki_summary", lambda _q: "合成詞條是一種合成資料。")
    assert lite_reply._try_wiki_lookup("合成詞條是什麼")
    assert reply_provenance.searched() is True
    reply_provenance.reset()
    monkeypatch.setattr(lite_reply, "_google_search_snippet", lambda _q: "經查，網傳停水訊息為假消息。")
    assert lite_reply._try_google_snippet("停水是真的嗎？")
    assert reply_provenance.searched() is True


def test_an_audio_transcription_is_quoted(monkeypatch):
    seen = []
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_download_content", lambda _id: b"synthetic audio bytes")
    monkeypatch.setattr(main.memory, "get_context", lambda *_a: [])
    monkeypatch.setattr(main.memory, "top_facts", lambda *a, **k: [])
    monkeypatch.setattr(main, "_get_persona_notes", lambda *_a: [])
    monkeypatch.setattr(main, "_thinking_indicator", lambda *_: nullcontext())
    monkeypatch.setattr(main, "_llm_chat", lambda parts, *_a: seen.append(parts) or "")
    event = SimpleNamespace(message=SimpleNamespace(id="AUD2"), reply_token="T_A2",
                            source=SimpleNamespace(user_id="U_TEST"))
    main._handle_audio_message(event, "G_A")
    assert any("原話一律放在「」裡" in part for part in seen[0] if isinstance(part, str))


def test_dates_around_weekday_marks_and_spoken_times_share_the_year():
    year = datetime.now(TW).year
    assert sorted(_events(f"{year + 2}年1月5日（三）18:00、1月6日（四）18:00 全家聚餐")) == [  # privacy-safe-fixture
        (f"{year + 2}-01-05", "18:00"), (f"{year + 2}-01-06", "18:00")]
    assert sorted(_events(f"{year + 2}年1月5日晚上7點、1月6日晚上7點 全家聚餐")) == [  # privacy-safe-fixture
        (f"{year + 2}-01-05", "19:00"), (f"{year + 2}-01-06", "19:00")]
    assert _events(f"{year - 1}年9月24日(三) 18:00 到 9月26日(五) 18:00 全家出遊") == []


def test_a_comma_does_not_carry_a_year_into_a_new_sentence():
    year = datetime.now(TW).year
    today = datetime.now(TW).date()
    expect = year if (10, 10) >= (today.month, today.day) else year + 1
    assert _events("阿公生日是1940年10月10日，10月10日 18:00 全家聚餐慶生") == [(f"{expect}-10-10", "18:00")]  # privacy-safe-fixture


# ── 11. round-3 review, seat 4 ───────────────────────────────────────────────

def test_a_claim_nobody_asked_about_gets_an_answer_or_nothing(monkeypatch, research_env):
    finished = []
    monkeypatch.setattr(main, "_finish_research_without_reply", lambda *a, **k: finished.append(True))
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _text: [])
    assert main._handle_web_research_question(research_env.event, "G_GR", "今天總統宣布停火了", addressed=False)
    assert research_env.sent == [] and finished == [True]
    # somebody asking still gets told nothing could be confirmed
    assert main._handle_web_research_question(research_env.event, "G_GR", "今天總統宣布停火了嗎")
    assert research_env.sent == [public_research.NO_EVIDENCE]


def test_an_attached_notice_may_be_cited(monkeypatch):
    cited = "根據健保署的公告，11月起掛號費調整。"
    monkeypatch.setattr("claude_client.chat", lambda *a, **k: cited)
    file_prompt = "請分析這個檔案的內容並回應。\n\n--- 內容開始 ---\n合成公告內容\n--- 內容結束 ---"
    assert main._llm_chat(file_prompt, [], [], None) == cited
    assert main._llm_chat("掛號費會漲嗎", [], [], None) == ""


# ── round-4 mutation gaps (2026-10-04): each case fails if its guard is removed ──

@pytest.mark.parametrize("reply", [
    "鑰匙找不到了，可能掉在車上。",  # an impersonal 找不到 with no lookup result
    "停車位找不到，只好停遠一點。",
    "收據上銀行的說明寫得很清楚。",  # the 據 in 收據／據點 is not a citation
    "這家銀行的據點官網都有列。",
    "系統查不到相關紀錄，要再打電話問客服。",  # an app's or a system's lookup, not the bot's
    "App查不到相關紀錄，可能還沒同步。",
    "App 裡查不到相關資料，晚點再試。",
    "根據官網的操作方式改密碼。",  # following published steps is an instruction
    "依照官方公告的流程辦理。",
    "網路上查到的資料有出入很正常，還是以醫生說的為準。",  # a general remark
    "查到的資料不一樣是正常的，問醫生最準。",
])
def test_round4_ordinary_sentences_are_not_search_claims(reply):
    assert not reply_policy.has_unbacked_search_claim(reply, searched=False, has_material=False)


@pytest.mark.parametrize("text", [
    "最近利率上漲，負債的人壓力很大",
    "最近物價上漲，聽說隔壁離婚了",
    "今年政策改了，打官司的費用變高",
    "最近經濟不好，很多人借錢度日",
])
def test_illness_legal_and_debt_words_keep_text_out_of_searches(text):
    assert not public_research.requires_current_research(text)
    assert public_research.search_query(text) == ""


@pytest.mark.parametrize("text", ["今天同學說總統宣布停火", "昨天隔壁老闆說總統今天訪美"])
def test_someone_else_as_the_subject_is_not_public_affairs(text):
    assert not public_research.requires_current_research(text)


# ── round-4 review (2026-10-04): replies H1 must not drop, claims it must still catch ──

@pytest.mark.parametrize("reply", [
    "我核對了你貼的菜單，兩道菜都有花生。",  # checking what the user supplied
    "我確認過你提供的兩個時間了，週四下午沒有重疊。",
    "我確認了你傳的照片，是同一家店。",
    "媽媽在網站查不到相關資料，明天再打客服。",  # a relative's lookup, with a place in between
    "爸爸用手機查不到相關紀錄，晚點再試。",
    "舅舅在官網上查不到相關公告，明天再問。",
    "查不到資料不代表消息是假的，先別轉傳。",  # general advice
])
def test_round4_useful_replies_are_not_dropped(reply):
    assert not reply_policy.has_unbacked_search_claim(reply, searched=False, has_material=False)


@pytest.mark.parametrize("reply", [
    "我確認過官網了，活動到月底。",
    "我確認過了，你說的活動已經取消。",
    "我核對過了，沒有這回事。",
    "我在網站查不到相關資料，應該是假的。",
    "網站查不到相關資料，應該是假的。",
    "在網站上查不到相關報導，應該是謠言。",
])
def test_round4_claims_are_still_caught(reply):
    assert reply_policy.has_unbacked_search_claim(reply, searched=False, has_material=False)


def test_dinner_answers_never_come_from_a_model(monkeypatch):
    # 2026-10-09: dinner picks come only from the verified list
    # (test_dinner_places.py); no model reply can be dropped or sent here.
    import dinner_places

    sent = []
    monkeypatch.setattr(main, "_reply", lambda _tok, text, **_k: sent.append(text))
    monkeypatch.setattr(main, "_llm_chat", lambda *_a, **_k: pytest.fail("dinner asked a model"))
    monkeypatch.setattr(dinner_places, "recommend", lambda *_a, **_k: dinner_places.NO_FRESH_TEXT)
    main._handle_dinner_recommendation(SimpleNamespace(reply_token="T_DINNER"), "G_DINNER")
    assert sent == [dinner_places.NO_FRESH_TEXT]


# ── 2026-10-05: the checks run on reply text while holding the GIL, so a run of
# 剛／沒有／在… must not backtrack exponentially, and a long clause with many
# matches or unclosed quotes must not be rescanned for each one ──

_JUNK = "ABCDEFGHIJK"
_SLOW_SHAPES = {
    "剛 run": lambda n: "我" + "剛" * n + "x",
    "剛 run opening a clause": lambda n: "好，" + "剛" * n + "x",
    "剛 run before a read": lambda n: "我" + "剛" * n + "看了x",
    "沒有 run": lambda n: "媽媽" + "沒有" * (n // 2) + "x查不到相關資料",
    "還沒有 run": lambda n: "媽媽" + "還沒有" * (n // 3) + "x查不到相關資料",
    "在家用 phrases": lambda n: "媽媽" + "在家用" * (n // 3) + _JUNK + "查不到相關資料",
    "person and 在 alternating": lambda n: "媽在" * (n // 2) + _JUNK + "查不到相關資料",
    "App and spaces": lambda n: "App" + " " * n + "x查不到相關資料",
    "unclosed quotes": lambda n: "「" * n + "查不到資料",
    "condition at the end": lambda n: "查不到報導" * (n // 5) + "如果",
    "caution at the end": lambda n: "查不到報導" * (n // 5) + "不一定",
    "question at the end": lambda n: "查不到報導" * (n // 5) + "？",
    "spaces after the clause": lambda n: "我查到" * (n // 6) + "，" + " " * (n // 2) + "就好",
    "caution, then spaces": lambda n: "查不到報導" * (n // 10) + "不一定，" + " " * (n // 2) + "好",
    "many short sentences": lambda n: "x。" * (n // 2),
    "many someone-else windows": lambda n: ("媽在" * 30 + "ABCDEFGHI媽查不到相關資料") * (n // 78),
    "many excused claims": lambda n: "你也查無" * (n // 4),
}


class _Deadline(Exception):
    pass


def _seconds(check, text):
    """How long ``check(text)`` takes; a hang fails after 5 s instead of stalling the run."""
    import signal
    import time

    def expire(*_):
        raise _Deadline(text[:12])

    previous = signal.signal(signal.SIGALRM, expire)
    outer = signal.setitimer(signal.ITIMER_REAL, 5)
    started = time.perf_counter()
    try:
        check(text)
        return time.perf_counter() - started
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if outer[0]:  # someone else's timer: give back what is left of it
            left = max(outer[0] - (time.perf_counter() - started), 0.001)
            signal.setitimer(signal.ITIMER_REAL, left, outer[1])


def _search_claim(text):
    return reply_policy.has_unbacked_search_claim(text, searched=False, has_material=False)


@pytest.mark.parametrize("shape", sorted(_SLOW_SHAPES))
def test_long_replies_are_checked_in_linear_time(shape):
    make = _SLOW_SHAPES[shape]
    small = min(_seconds(_search_claim, make(5000)) for _ in range(3))
    large = min(_seconds(_search_claim, make(20000)) for _ in range(3))
    assert large < 1.0  # 28 剛 took 1.9 s before, ×4 for every extra 剛; a hang stops at 5 s
    assert large < 10 * max(small, 0.002)  # 4× the text should take about 4× the time, not 16×


def test_a_run_of_剛_does_not_stall_the_change_claim_check():
    assert _seconds(reply_policy.strip_operation_claims, "好，" + "剛" * 20000 + "x") < 0.5


@pytest.mark.parametrize("text, masked", [
    ("「a「b」c」", "「〇〇〇」c」"),  # the first 」 closes it, as before
    ("「「a", "「「a"),  # never closed: left alone
    ('"a"b"', '"〇"b"'),
    ("他說「查不到。請重試」，「沒有", "他說「〇〇〇〇〇〇〇」，「沒有"),
    ("『x』“y”「z", "『〇』“〇”「z"),
])
def test_quotes_are_masked_as_before(text, masked):
    assert reply_policy._mask_quotes(text) == masked


def test_someone_else_counts_within_80_characters_of_the_search():
    near = "媽媽" + "在家" * 39 + "查不到相關資料"  # 媽媽 starts 80 characters before 查
    far = "媽媽" + "在家" * 40 + "查不到相關資料"  # 82: outside the window, so the bot's claim
    assert not _search_claim(near)
    assert _search_claim(far)


@pytest.mark.parametrize("reply", [
    "媽媽在台北的家裡剛剛已經查到相關資料，明天再一起看。",  # a long 在 phrase, then more words
    "阿姨用剛買的手機並未查到相關報導，晚點再問。",
    "他們之前也都還沒有上網查到相關資料，先等等。",
    "姑姑在醫院候診區用手機剛才查到相關資料。",
    "App  裡面查不到相關資料，晚點再試。",
    "網路上查不到相關報導反正查不到這件事就算了。",  # 就算了 comes after both
])
def test_someone_elses_search_still_reads_the_same(reply):
    assert not _search_claim(reply)


@pytest.mark.parametrize("reply", [
    "我剛剛才查了，沒有這回事。",  # 剛 + 剛才
    "我剛剛剛剛查過了，沒有這回事。",
    "好，剛剛特地上網查了一下，沒有這回事。",
    "網路上查不到相關報導就算了反正查不到這件事。",  # 就算了 excuses only the first
])
def test_the_bots_own_search_claims_still_read_the_same(reply):
    assert _search_claim(reply)


def _splits_one_way(words):
    """Sardinas–Patterson: every string made of these words splits into them in one way only."""
    words = set(words)

    def tails(prefixes, wholes):
        return {w[len(p):] for p in prefixes for w in wholes if w.startswith(p) and w != p}

    dangling, seen = tails(words, words), set()
    while dangling and frozenset(dangling) not in seen:
        if dangling & words:
            return False
        seen.add(frozenset(dangling))
        dangling = tails(dangling, words) | tails(words, dangling)
    return True


def _alternatives(group):
    assert group.startswith("(?:") and group.endswith(")"), group
    words = group[3:-1].split("|")
    assert all(re.fullmatch(r"[^\\()\[\]{}?*+.|^$]+", w) for w in words), group
    return words


@pytest.mark.parametrize("name", ["_HOW", "_BY_YOU_WORD", "_BY_OTHERS_WORD"])
def test_repeated_words_split_one_way(name):
    # 剛剛 next to 剛 made a run of 剛 backtrack exponentially (2026-10-05); 才剛
    # next to 剛才 and 剛 would do the same to 剛才剛…, and the timing tests
    # alone would not notice.
    assert _splits_one_way(_alternatives(getattr(reply_policy, name)))


def test_the_change_claim_lead_words_split_one_way():
    lead = re.search(r"\\s\*\(\?:([^()]*)\)\*\(\?:已經\|已\)", reply_policy._OPERATION_CLAIM_RES[4].pattern)
    assert lead, "_OPERATION_CLAIM_RES[4] changed shape: update this test"
    assert _splits_one_way(lead.group(1).split("|"))


def test_the_one_way_check_catches_ambiguous_words():
    assert not _splits_one_way(["剛剛", "剛"])
    assert not _splits_one_way(["剛才", "剛", "才剛"])
    assert not _splits_one_way(["a", "ab", "ba"])
    assert _splits_one_way(["剛才", "剛", "已經", "已"])


def test_a_condition_right_after_the_claim_counts_as_before():
    # 「…說的話」: right after the match, 的話 counts even after 說, as when the
    # rest of the clause was matched as a string of its own
    assert not reply_policy._claim_matches("查說的話好", (re.compile("查說"),), first_person=False)


def test_by_others_reads_like_the_plain_alternation():
    # The 在／用 pieces are atomic (2026-10-05): every short string must still get
    # the verdict of the plain, backtracking form of the same words and phrase.
    import itertools

    pattern = reply_policy._SEARCH_BY_OTHERS_RE.pattern
    word = reply_policy._BY_OTHERS_WORD
    phrase = "[在用][^在用，,。；;：:！？!?\\n]{0,8}"
    atomic = word + "*(?>" + phrase + word + "*(?=[在用]|$))*"
    assert pattern.count(atomic) == 1, "_SEARCH_BY_OTHERS_RE changed shape: update this test"
    plain = re.compile(pattern.replace(atomic, "(?:" + word[3:-1] + "|" + phrase + ")*"))
    tokens = ["媽媽", "你", "剛", "才", "沒", "有", "在", "用", "家", "台北市信義區", "x"]
    for n in range(1, 6):
        for parts in itertools.product(tokens, repeat=n):
            text = "".join(parts)
            assert bool(reply_policy._SEARCH_BY_OTHERS_RE.search(text)) == bool(plain.search(text)), text
