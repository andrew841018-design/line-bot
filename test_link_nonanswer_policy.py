"""User-rejected linked-content nonanswers must never reach LINE."""
from types import SimpleNamespace

import pytest

import main
import line_push_client
import output_validator
import public_research


REJECTED = (
    "這個連結讀不到任何具體內容，只有 YouTube Shorts 的一般資訊。\n\n"
    "因此，我無法根據您提供的連結判斷其提及的問題或主張。\n"
    "如果方便，請提供影片的標題或更多描述，我會盡力協助查找。"
)


@pytest.mark.parametrize("text", [
    REJECTED,
    REJECTED + "\n來源：https://www.youtube.com/shorts/DEMO1234567",
    REJECTED + "\n來源：YouTube Shorts",
    REJECTED + "希望這有幫助！",
    "我無法讀取這個 YouTube Shorts 的具體內容，因此無法判斷影片主張。如果您方便提供標題或描述，我可以協助查找。",
    "這個連結讀不到內容。這是一個 YouTube Shorts 短影音連結。",
    "這個 YouTube Shorts 連結讀不到內容。請提供標題或截圖，才能判斷。",
    "這個連結讀不到內容。請稍後再試。",
    "只有 YouTube Shorts 的一般資訊，沒有取得影片的具體內容。",
    "這個連結讀不到內容，請補充影片標題或描述。",
    "無法根據這個影片連結判斷主張，請提供更多描述。",
    "這個 YouTube 連結目前只取得 metadata，未取得逐字稿。",
    "這則回覆在送出前被擋下：YouTube 連結解析流程沒有正確啟動。請重新貼一次連結，我會改用 yt-dlp、oEmbed、HTML metadata 重新抓取。",
    # 2026-09-26: 「沒字幕、無法判斷」with nothing else — Andrew: 這種狀況就別回
    "由於沒有取得影片的字幕或逐字稿，無法判斷其具體論述和支持觀點的證據。因此，無法進一步核實影片中關於某地買車優勢的具體主張。",
    "這支影片沒有字幕，無法判斷真假。",
    "未能取得影片字幕，因此無法核實影片的說法。",
    "影片沒有取得字幕或逐字稿，我無法判斷影片說法是否正確。",
    "只看得到影片標題和描述，無法確認影片的主張。",
    "因為沒有字幕，無法判斷影片內容。",
    "這支影片沒有字幕，無法判斷影片中的數據是否正確。",
    "這支影片沒有字幕，無法核實影片提及的研究。",
    "這支影片沒有字幕，無法判斷：沒有逐字稿。",
    # 「建議」 must not rescue a request for the user to supply the title.
    "沒有取得影片字幕，建議請你提供影片標題或截圖。",
])
def test_rejected_nonanswer_is_empty(text):
    result = output_validator.validate_outbound_text(text)
    assert not result.ok
    assert result.text == ""


@pytest.mark.parametrize("text", [
    "無法讀取 YouTube 影片內容，但央行已宣布降息 1 碼。",
    "未取得 YouTube 影片逐字稿但央行已宣布降息一碼。",
    "未取得 YouTube 影片的逐字稿，央行已宣布降息一碼。",
    "影片主題是央行利率決策，目前只取得標題與描述，未取得逐字稿。",
    "目前只取得 YouTube 影片標題與描述，主題是央行利率決策。",
    "這個 YouTube 影片打不開是因為作者設為私人，公開說明顯示只有獲邀帳號能觀看。",
    "這個 YouTube 連結打不開，清除快取後再試。",
    "未取得完整逐字稿；根據標題、頻道與描述，這場直播主題是在說央行利率決策。",
    "要設定觀看 YouTube 影片的提醒，請提供影片標題和提醒時間。",
    "目前有兩筆活動，無法判斷要修改哪一筆，請提供日期。",
    public_research.NO_EVIDENCE,
    # A judgement or advice next to the missing-subtitle note is still an answer.
    "單一個案無法證實影片內容具有普遍療效。",
    "無法查證影片說法時應先比對原始公告再轉傳。",
    "這支影片沒有字幕，但標題寫的補助金額已過時，現在是每月三千元。",
    "這支影片沒有字幕所以建議先核對原始公告。",
    "只有標題寫錯了，影片內容是對的。",
    "沒有取得字幕；無法查證影片說法時應先比對原始公告再轉傳。",
    "這支影片只有標題寫錯了。",
    "這支影片沒有字幕也不影響操作：按右下角齒輪即可切換音軌。",
    "這支影片沒有字幕會讓聽障者難以理解口白。",
    "這支影片沒有字幕，無法判斷真假因為標題數字與官方統計不符。",
    "這支影片未取得字幕所以建議先核對原始公告。",
    "這支影片沒有字幕，無法證實影片宣稱的療效：引用的研究只有十名受試者且沒有對照組。",
    "這支影片沒有字幕，無法判斷完整論述是否正確：標題把年利率誤寫成月利率。",
])
def test_useful_answers_and_other_domains_survive(text):
    result = output_validator.validate_outbound_text(text)
    assert result.ok
    assert result.text == text


@pytest.mark.parametrize("muted", [False, True])
def test_reply_suppression_is_durable_without_line_or_memory(monkeypatch, muted):
    monkeypatch.setattr(main.settings, "bot_muted", muted)
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: False)
    monkeypatch.setattr(main, "ApiClient", lambda *a: pytest.fail("LINE must not be called"))
    main.memory.begin_inbound_event("G_TEST", "M_TEST")
    main._register_inbound_reply_batch("T_TEST", "G_TEST", ["M_TEST"])
    main._append_bot_turn("G_TEST", REJECTED)
    assert main._reply("T_TEST", REJECTED, group_id="G_TEST", include_auxiliary=False) is False
    assert main.memory.get_inbound_event_status("G_TEST", "M_TEST") == "completed_no_reply"
    assert main.memory.get_context("G_TEST") == []


def test_one_shot_nonanswer_is_purged_and_terminal(monkeypatch):
    monkeypatch.setattr(main, "_load_one_shot_replies", lambda: {"G_TEST": REJECTED})
    saved = []
    monkeypatch.setattr(main, "_save_one_shot_replies", lambda data: saved.append(dict(data)))
    monkeypatch.setattr(main, "ApiClient", lambda *a: pytest.fail("LINE must not be called"))
    main.memory.begin_inbound_event("G_TEST", "M_TEST")
    main._register_inbound_reply_batch("T_TEST", "G_TEST", ["M_TEST"])
    assert main._try_one_shot_reply(SimpleNamespace(reply_token="T_TEST"), "G_TEST")
    assert saved == [{}]
    assert main.memory.get_inbound_event_status("G_TEST", "M_TEST") == "completed_no_reply"


@pytest.mark.parametrize("kind", ["text", "textV2"])
def test_shared_push_payload_drops_rejected_text(kind):
    assert line_push_client._validated_message_dict(
        {"type": kind, "text": REJECTED}, source="synthetic-test",
    ) is None
