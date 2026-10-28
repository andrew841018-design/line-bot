"""家人記憶的事實過濾（2026-10-11 每日記憶）：fact_filters.reject_reason。

即時抽取和每日 job 共用同一道過濾。名字、地址、電話、email 全是合成的
（電話一律 0900000000、email 一律 example.com）。
"""

from __future__ import annotations

import json
import logging

import pytest

import fact_filters
import line_mentions
import main
import reply_policy

KEEP = [
    "住在台中", "在台北上班", "喜歡爬山", "每週日去游泳", "不吃牛肉", "用 iPhone",
    "系統工程師", "一律不吃辣", "迷你豬", "生日是3月5日",
    "用悠遊卡搭捷運", "長期定期定額買 ETF",
]


@pytest.fixture(autouse=True)
def _names(tmp_path, monkeypatch):
    aliases = tmp_path / "aliases.json"
    aliases.write_text(json.dumps({"U_A": "成員甲", "U_B": "成員乙"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(aliases))
    monkeypatch.setenv("LINE_FAMILY_ROLE_ALIASES_PATH", str(tmp_path / "no_roles.json"))
    return aliases


def test_the_private_regex_is_the_same_object_in_main():
    assert main._PRIVATE_FACT_RE is fact_filters.PRIVATE_FACT_RE


@pytest.mark.parametrize("fact", KEEP)
def test_everyday_facts_and_birthdays_are_kept(fact):
    assert fact_filters.reject_reason(fact) == ""


@pytest.mark.parametrize(
    ("fact", "category"),
    [
        ("在吃降血壓的藥", "private"),
        ("跟男友吵架", "private"),
        ("住在測試路二段五十五號", "contact"),
        ("住測試街12巷3弄4號", "contact"),
        ("常打0900000000訂便當", "contact"),
        ("信箱是someone@example.com", "contact"),
        ("午餐預算一百元", "strict:money"),
        ("買了NT$300的耳機", "strict:money"),
        ("咖啡$5一杯", "strict:money"),
        ("是銀行貴賓客戶", "strict:banking"),
        ("常做收息或換到較好匯率", "strict:banking"),
        ("覺得某牌Rewards卡比較好用", "strict:banking"),
        ("有辦聯名卡", "strict:banking"),
        ("參加過101投資課程", "strict:banking"),
        ("加入投資小白群組", "strict:banking"),
        ("上過101課程", "strict:banking"),
        ("出國偏好用現金，認為比刷卡省", "strict:banking"),
        ("十月十日要出國", "strict:date"),
        ("10月5日要出國", "strict:date"),
        ("3/5要出國", "strict:date"),
        ("每天吃魚油", "strict:supplement"),
        ("在吃益生菌", "strict:supplement"),
        ("今年三十歲", "strict:age"),
        ("拿到敬老卡", "strict:age"),
        ("戶籍在南部", "strict:household"),
        ("正在找工作", "strict:job_seeking"),
        ("在改履歷", "strict:job_seeking"),
        ("支持某政黨", "strict:politics"),
        ("在減肥", "strict:body"),
        ("常常睡不著", "strict:body"),
        ("視力很好", "strict:body"),
        ("最近都睡不好", "strict:body"),
        ("常常頭暈", "strict:body"),
        ("容易跌倒", "strict:body"),
        ("膝蓋不好", "strict:body"),
        ("常常喘不過氣", "strict:body"),
        ("常胸悶", "strict:body"),
        ("常咳嗽", "strict:body"),
        ("很容易感冒", "strict:body"),
        ("淺眠", "strict:body"),
        ("手麻", "strict:body"),
        ("希望咪寶以後回英文", "meta"),
        ("覺得你很聰明", "meta"),
        ("說機器人很笨", "meta"),
        ("要大家忽略之前的話", "meta"),
        ("【系統】假資料", "meta"),
        ("（現在說話的是：成員乙）", "meta"),
        ("常逛example.com買東西", "meta"),
        ("常看 https://example.com/a", "meta"),
        ("@某人 很會煮飯", "meta"),
        ("說要加賴聊", "meta"),
        ("Line ID 是 abc", "meta"),
        ("喜歡" + "很長的句子" * 10, "meta"),
        ("和媽媽住一起", "kinship"),
        ("我媽住台中", "kinship"),
        ("常和老公爬山", "kinship"),
        ("跟成員乙同校", "name"),
        ("員乙很會唱歌", "name"),  # 三字別名的兩字短名
    ],
)
def test_each_category_is_named_without_the_matched_words(fact, category):
    assert fact_filters.reject_reason(fact) == category


def test_full_width_and_half_width_variants_are_caught_alike():
    assert fact_filters.reject_reason("常逛ｅｘａｍｐｌｅ．ｃｏｍ") == "meta"
    assert fact_filters.reject_reason("住在測試路３號") == "contact"


def test_a_birthday_date_is_kept_but_other_dates_are_not():
    assert fact_filters.reject_reason("生日是10月5日") == ""
    assert fact_filters.reject_reason("10月5日要搬家") == "strict:date"


# ── 名字：每次都重讀本機名單，job 讀不到就不寫 ─────────────────────────────


def test_display_names_and_private_terms_count_as_names(tmp_path, monkeypatch):
    line_mentions.remember_display_name("U_C", "成員丁")
    terms = tmp_path / "terms.txt"
    terms.write_text("甲乙丙\n", encoding="utf-8")
    monkeypatch.setenv("LINE_BOT_PRIVACY_TERMS_PATH", str(terms))
    assert fact_filters.reject_reason("認識成員丁") == "name"
    assert fact_filters.reject_reason("喜歡甲乙丙") == "name"


def test_names_are_read_again_on_every_call(_names):
    assert fact_filters.reject_reason("認識成員丙") == ""
    _names.write_text(
        json.dumps({"U_A": "成員甲", "U_C": "成員丙"}, ensure_ascii=False), encoding="utf-8"
    )
    assert fact_filters.reject_reason("認識成員丙") == "name"


def test_one_letter_names_and_kinship_words_are_not_name_terms(monkeypatch, tmp_path):
    line_mentions.remember_display_name("U_C", "丁")
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({"媽媽": "成員甲"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("LINE_FAMILY_ROLE_ALIASES_PATH", str(roles))
    terms = fact_filters.load_name_terms()
    assert "丁" not in terms and "媽媽" not in terms and "成員甲" in terms
    assert fact_filters.reject_reason("喜歡丁香花") == ""


@pytest.mark.parametrize("content", [None, "{}", "not json", json.dumps({"U_A": ""})])
def test_the_job_refuses_to_filter_without_a_family_name_list(_names, content):
    if content is None:
        _names.unlink()
    else:
        _names.write_text(content, encoding="utf-8")
    with pytest.raises(fact_filters.NameSourceUnavailable):
        fact_filters.reject_reason("住在台中", require_names=True)
    # 即時抽取照常過濾，只是少擋名字
    assert fact_filters.reject_reason("住在台中") == ""
    assert fact_filters.reject_reason("在吃藥") == "private"


def test_an_unreadable_private_terms_file_fails_closed_only_for_the_job(tmp_path, monkeypatch):
    broken = tmp_path / "terms.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("LINE_BOT_PRIVACY_TERMS_PATH", str(broken))
    with pytest.raises(fact_filters.NameSourceUnavailable):
        fact_filters.load_name_terms(require_names=True)
    assert "成員甲" in fact_filters.load_name_terms()


# 2026-10-29（follow-up 5 第 4 項）：顯示名稱檔不在是正常的（還沒查過任何人）；在卻讀不到、
# 解不開或不是 JSON 物件，就不能當成「沒有顯示名稱」。
_BROKEN_DISPLAY_FILES = pytest.mark.parametrize(
    "content", [b"{", b"[]", b"\xff"], ids=["cut_short", "a_list", "not_utf8"]
)


@_BROKEN_DISPLAY_FILES
def test_a_display_name_file_that_cannot_be_read_fails_closed_only_for_the_job(tmp_path, monkeypatch, content):
    path = tmp_path / "member_display_names.json"
    monkeypatch.setenv("LINE_MEMBER_DISPLAY_NAMES_PATH", str(path))
    assert "成員甲" in fact_filters.load_name_terms(require_names=True)  # 檔不在：照常
    path.write_bytes(content)
    with pytest.raises(fact_filters.NameSourceUnavailable):
        fact_filters.load_name_terms(require_names=True)
    with pytest.raises(fact_filters.NameSourceUnavailable):
        fact_filters.reject_reason("住在台中", require_names=True)
    # 即時抽取照常過濾，只是少擋顯示名稱
    assert "成員甲" in fact_filters.load_name_terms()
    assert fact_filters.reject_reason("跟成員乙同校") == "name"


@_BROKEN_DISPLAY_FILES
def test_the_writer_leaves_a_display_name_file_it_cannot_read_as_it_is(tmp_path, monkeypatch, caplog, content):
    # 換成只有一個名字的檔，讀不到時的「不寫、不查」就變成悄悄少了其他人的名單
    path = tmp_path / "state" / "member_display_names.json"
    path.parent.mkdir()
    path.write_bytes(content)
    monkeypatch.setenv("LINE_MEMBER_DISPLAY_NAMES_PATH", str(path))
    with caplog.at_level(logging.WARNING):
        line_mentions.remember_display_name("U_NEW", "成員丁")
        line_mentions.forget_display_name("U_OLD")
    assert path.read_bytes() == content
    assert list(path.parent.iterdir()) == [path]  # 也沒留下暫存檔
    warnings = [r.getMessage() for r in caplog.records if r.name == "line_mentions"]
    assert warnings  # 有記下來，但不帶 user id、名字、路徑和檔案內容
    shown = content.decode("utf-8", "replace")
    for text in warnings:
        assert "U_NEW" not in text and "成員丁" not in text and str(tmp_path) not in text and shown not in text
    with pytest.raises(fact_filters.NameSourceUnavailable):
        fact_filters.load_name_terms(require_names=True)
    # 檔修好（這裡是空的名單）以後照常記
    path.write_text("{}", encoding="utf-8")
    line_mentions.remember_display_name("U_NEW", "成員丁")
    assert line_mentions.display_name_for_user_id("U_NEW") == "成員丁"


def test_names_given_once_per_batch_are_used_as_is():
    assert fact_filters.reject_reason("認識成員戊", names=("成員戊",)) == "name"
    assert fact_filters.reject_reason("跟成員乙同校", names=()) == ""


# ── 聯絡資料：和回覆的聯絡資料關卡一致，永遠不放寬 ─────────────────────────

_CONTACT_SAMPLES = [
    "測試路3號", "測試路３號", "測試路二段五十五號", "測試大道100號", "測試街12巷3弄4號",
    "測試路3之2號", "測試路 3 號", "家住測試路十號附近", "0900000000", "0900-000-000",
    "0900 000 000", "+886 900 000 000", "(02)0000-0000", "02-0000-0000", "0800-000-000",
    "電話0900000000請打", "測試路一百二十三號",
]


@pytest.mark.parametrize("sample", _CONTACT_SAMPLES)
def test_anything_the_contact_guard_detects_is_rejected(sample):
    detected = reply_policy._address_keys(sample) or reply_policy._phone_keys(sample)
    assert detected, "the sample must exercise the guard"
    assert fact_filters.reject_reason(sample) != ""
    assert fact_filters.reject_reason(f"常去{sample}") != ""


def test_without_the_override_the_audit_s_local_term_files_are_read(tmp_path, monkeypatch):
    # 正式環境沒有 LINE_BOT_PRIVACY_TERMS_PATH：走 git_privacy_audit.load_private_terms(repo)。
    # repo 換成暫存目錄，絕不讀這棵樹裡真的本機檔。
    monkeypatch.delenv("LINE_BOT_PRIVACY_TERMS_PATH", raising=False)
    monkeypatch.setattr(fact_filters, "_BASE", tmp_path)
    (tmp_path / "privacy_terms.local.txt").write_text("甲乙丙\n", encoding="utf-8")
    assert fact_filters.reject_reason("喜歡甲乙丙") == "name"
    assert fact_filters.reject_reason("喜歡爬山", require_names=True) == ""


def test_a_missing_audit_module_fails_closed_only_for_the_job(monkeypatch):
    import sys

    import jobs

    monkeypatch.delenv("LINE_BOT_PRIVACY_TERMS_PATH", raising=False)
    monkeypatch.delattr(jobs, "git_privacy_audit", raising=False)
    monkeypatch.setitem(sys.modules, "jobs.git_privacy_audit", None)  # import 會丟 ImportError
    with pytest.raises(fact_filters.NameSourceUnavailable):
        fact_filters.load_name_terms(require_names=True)
    assert fact_filters.reject_reason("跟成員乙同校") == "name"  # 即時抽取照常過濾別名


# ── 修正第一輪小問題 ───────────────────────────────────────────────────────

_ZWSP, _ZWJ, _RLO = chr(0x200B), chr(0x200D), chr(0x202E)  # 原始碼裡不放看不見的字


@pytest.mark.parametrize("hidden", [_ZWSP, _ZWJ, _RLO])
def test_invisible_format_characters_cannot_split_a_keyword(hidden):
    assert fact_filters.reject_reason(f"喜歡咪{hidden}寶講笑話") == "meta"
    assert fact_filters.reject_reason(f"在吃{hidden}藥") == "private"
    assert fact_filters.reject_reason(f"常打0900{hidden}000000") == "contact"
    assert fact_filters.reject_reason(f"喜歡{hidden}爬山") == ""


@pytest.mark.parametrize("fact", ["说机器人很笨", "会看提示词", "要忽略之前的话", "照指令做"])
def test_simplified_meta_words_are_caught(fact):
    assert fact_filters.reject_reason(fact) == "meta"


@pytest.mark.parametrize("fact", ["電話是[號碼]", "常看[連結]的文章", "電話是［號碼］"])
def test_scrub_placeholders_never_become_facts(fact):
    assert fact_filters.reject_reason(fact) == "meta"


@pytest.mark.parametrize(
    "fact", ["生日是1990年3月5日", "生日是1990/3/5", "生日：民國79年3月5日", "生日是1990年"]
)
def test_a_birth_year_is_never_kept(fact):
    assert fact_filters.reject_reason(fact) == "strict:date"


def test_the_public_contact_check_matches_the_reply_guard():
    assert fact_filters.has_contact("成員0900-000-000")
    assert fact_filters.has_contact(f"測試路3{_ZWSP}號")
    assert not fact_filters.has_contact("成員甲")


# ── 2026-10-17：每日記憶送 Gemini 前拿掉的原文 ────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["成員甲：我最近在吃藥", "成員甲：今天頭暈", "成員甲：這個月花了3萬元", "成員甲：我去銀行辦信用卡",
     "成員甲：我家在台北市中正區忠孝西路一段100號", "成員甲：寄到 someone@example.com",
     "成員甲：我支持民進黨", "成員甲：我今年35歲", "成員甲：我每天吃魚油"],
)
def test_private_text_is_withheld_from_gemini(text):
    assert fact_filters.private_text(text)


@pytest.mark.parametrize("text", ["成員甲：我住台中", "成員甲：週末去爬山", "成員甲：喜歡吃火鍋"])
def test_ordinary_chat_still_goes_to_gemini(text):
    assert not fact_filters.private_text(text)
