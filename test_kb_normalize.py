"""jobs/kb_normalize：地點知識庫的嚴格解析（2026-10-31，第一批段一）。全部用合成資料。

含實作審查（審查員 1、3）實測過的反例。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "jobs"))
import kb_normalize as kbn  # noqa: E402


@pytest.mark.parametrize("raw", [
    "02-2345-6789", "(02)2345-6789", "02 2345 6789", "02.2345.6789", "02–2345–6789", "02‐2345‐6789",
    "０２－２３４５－６７８９", "+886 2 2345 6789", "+886 (2) 2345-6789", "+886-(0)2-2345-6789",
    "02-2345-6789 轉 123", "02-2345-6789#12",
])
def test_landline_formats_normalize_to_digits(raw):
    assert kbn.parse_phone(raw) == "0223456789"


@pytest.mark.parametrize("raw, digits", [
    ("0900-000-000", "0900000000"), ("09-0000-0000", "0900000000"), ("0900 00 0000", "0900000000"),
    ("+886 900 000 000", "0900000000"), ("0800-012-345", "0800012345"), ("03-123-4567", "031234567"),
    ("04-2234-5678", "0422345678"), ("049-223-4567", "0492234567"),
])
def test_mobile_and_other_formats(raw, digits):
    assert kbn.parse_phone(raw) == digits


@pytest.mark.parametrize("raw", [
    "2345-6789", "1234", "0912-345-67", "5520 0912 3456 7890", "02-2345-6789-1234-5678", "0123456789",
    "02-2345​-6789", "電話02-2345-6789", "02-2345-678", "0.912345678",
    "09" "١٢٣٤٥٦٧٨",
])
def test_not_phones(raw):
    assert kbn.parse_phone(raw) is None


@pytest.mark.parametrize("text", [
    "卡號 4000 0900 000 000", "訂單 #0223456789", "訂單編號 0900000000 已出貨", "統編 0223456789",
    "取貨號碼 0900000000",
])
def test_find_phones_skips_card_and_order_numbers(text):
    assert kbn.find_phones(text) == []


def test_find_phones_reports_digits():
    assert [d for _s, _e, d in kbn.find_phones("訂位 02-2345-6789，手機 0900-000-000")] == ["0223456789", "0900000000"]


@pytest.mark.parametrize("raw, expected", [
    ("台北市中正區重慶南路一段122號", "台北市中正區重慶南路1段122號"),
    ("臺北市中正區重慶南路1段122號", "台北市中正區重慶南路1段122號"),
    ("100台北市中正區光復里重慶南路1段122號3樓", "台北市中正區重慶南路1段122號"),
    ("台北市大安區新生南路三段十二巷五弄七號", "台北市大安區新生南路3段12巷5弄7號"),
    ("新竹縣竹北市光明六路10號之3", "新竹縣竹北市光明六路10號之3"),
    ("台中市西區民生路3-2號", "台中市西區民生路3之2號"),
    ("台 北 市 中 正 區 重 慶 南 路 1 段 122 號", "台北市中正區重慶南路1段122號"),
    ("高雄市前鎮區中山二路1號", "高雄市前鎮區中山二路1號"),
    ("桃園市平鎮區環南路1號", "桃園市平鎮區環南路1號"),
    ("台南市新市區中正路1號", "台南市新市區中正路1號"),
    ("台中市大里區大里東路1號", "台中市大里區大里東路1號"),
])
def test_full_addresses(raw, expected):
    assert kbn.parse_address(raw) == expected


@pytest.mark.parametrize("raw", [
    "中山路3号", "大同村5鄰12號", "中正區重慶南路1段122號", "台北市重慶南路1段122號", "台北市中正區重慶南路",
    "台北市中正區重慶南路1段122號\n", "台北市中正區重慶南路1段1​22號", "台北市中正區重慶南路1段١٢號",
])
def test_incomplete_or_odd_addresses_are_rejected(raw):
    assert kbn.parse_address(raw) is None


def test_similar_roads_and_cities_stay_different():
    assert kbn.parse_address("台北市大安區民生東路1號") != kbn.parse_address("台北市大安區新生東路1號")
    assert kbn.parse_address("台北市中山區中山北路二段1號") != kbn.parse_address("台中市北區中山北路二段1號")
    assert kbn.parse_address("台北市中正區中山路1號之2") != kbn.parse_address("台北市中正區中山路1號之3")
    assert kbn.region("台北市中正區重慶南路1段122號") == "台北市中正區"


FULL = "台北市中正區重慶南路1段122號"


@pytest.mark.parametrize("text, found, with_district, with_city", [
    ("就在中正區重慶南路一段 122 號", True, True, False),
    ("北門郵局台北市中正區重慶南路1段122號", True, True, True),
    ("地址：重慶南路一段122號（近捷運站）", True, False, False),
    ("位於重慶南路1段122號", True, False, False),
    ("台北市中正區重慶南路1段122號3樓", True, True, True),
    ("台北市中正區重慶南路1段122號 02-2345-6789", True, True, True),
    ("台北市重慶南路1段122號", True, False, False),
    ("100台北市中正區重慶南路1段122號", True, True, True),
    ("台中市北區重慶南路1段122號", False, False, False),
    ("台北市大安區重慶南路1段122號", False, False, False),
    ("台北市中正區重慶北路1段122號", False, False, False),
    ("台北市中正區重慶南路1段1122號", False, False, False),
    ("台北市中正區重慶南路1段122號之3", False, False, False),
    ("台北市中正區重慶南路1段", False, False, False),
    ("新重慶南路1段122號", False, False, False),
])
def test_address_spans_in_page_text(text, found, with_district, with_city):
    spans = kbn.address_spans(text, FULL)
    assert bool(spans) is found
    if found:
        assert any(s.district for s in spans) is with_district
        assert any(s.city for s in spans) is with_city


def test_address_spans_cover_postal_code_city_and_floor():
    # 郵遞區號、縣市、樓層都算在地址那段裡：店名和地址之間、地址和電話之間就不會留下數字或縣市
    text = kbn.address_text("地址：100台北市中正區重慶南路1段122號3樓之1 電話")
    [span] = kbn.address_spans(text, FULL)
    assert text[span.start:span.end] == "100台北市中正區重慶南路1段122號3樓之1"


def test_district_names_are_not_suffix_matched():
    full = kbn.parse_address("台南市南區中山路1號")
    assert not kbn.address_spans("台南市安南區中山路1號", full)
    assert kbn.address_spans("台南市南區中山路1號", full)
    other = kbn.parse_address("高雄市前鎮區中山二路1號")
    assert not kbn.address_spans("苓雅區中山二路1號", other)


@pytest.mark.parametrize("raw, kind, ok", [
    ("甲麵館", "餐廳", True),
    ("鼎泰豐", "餐廳", False),
    ("鼎泰豐信義店", "餐廳", True),
    ("全家便利商店", "店家", False),
    ("全家便利商店民生店", "店家", True),
    ("全家福海鮮餐廳", "餐廳", True),
    ("7-ELEVEN", "店家", False),
    ("711民生門市", "店家", True),
    ("Starbucks", "餐廳", False),
    ("Starbucks 民生門市", "餐廳", True),
    ("Seven Seas Bistro", "餐廳", True),
    ("Mosaic Cafe", "餐廳", True),
    ("五十嵐", "店家", False),
    ("郵局", "郵局", False),
    ("台北北門郵局", "郵局", True),
    ("台北北門郵局", "店家", False),
    ("中正區公所", "機關", True),
    ("台北監理所", "機關", True),
    ("甲​麵館", "餐廳", False),
    ("甲\U000e0041麵館", "餐廳", False),
    ("甲麵館 https://x.com", "餐廳", False),
    ("甲", "餐廳", False),
    # 連鎖品牌加上類別字還是要寫分店（最終版審查員 1）；全家福是明列的例外
    ("全聯福利中心", "店家", False),
    ("爭鮮迴轉壽司", "餐廳", False),
    ("王品牛排", "餐廳", False),
    ("八方雲集鍋貼", "餐廳", False),
    ("必勝客披薩", "餐廳", False),
    ("屈臣氏藥妝", "店家", False),
    ("50嵐飲料店", "店家", False),
    ("八方雲集鍋貼水餃專賣店", "餐廳", False),
    ("鼎泰豐分店", "餐廳", False),
    ("50嵐復興店", "店家", True),
    ("全聯福利中心復興店", "店家", True),
    ("全聯復興店", "店家", True),
    ("王品牛排台中中港店", "餐廳", True),
    ("全家福小吃", "餐廳", True),
    # 分店寫在一對括號裡也算（括號裡的類別字照樣不算分店）
    ("鼎泰豐(信義店)", "餐廳", True),
    ("鼎泰豐（信義店）", "餐廳", True),
    ("全聯福利中心(復興店)", "店家", True),
    ("50嵐(飲料店)", "店家", False),
    ("鼎泰豐()", "餐廳", False),
    ("鼎泰豐(信義店)(大安店)", "餐廳", False),
])
def test_names(raw, kind, ok):
    name, why = kbn.validate_name(raw, kind)
    assert (name is not None) is ok, why


def test_hidden_chars_allow_emoji_joiners_only_when_asked():
    family = "我們去甲麵館👨‍👩‍👧"
    assert kbn.has_hidden_chars(family) and not kbn.has_hidden_chars(family, emoji_ok=True)
    assert kbn.has_hidden_chars("甲‮麵館", emoji_ok=True)


@pytest.mark.parametrize("url, site", [
    ("https://www.google.com.tw/maps/place/x", "google"),
    ("https://www.google.com/maps/place/x", "google"),
    ("https://m.facebook.com/x", "facebook"),
    ("https://ifoodie.tw/restaurant/1", "ifoodie"),
    ("https://www.tripadvisor.com/x", "tripadvisor"),
    ("https://www.tripadvisor.com.tw/x", "tripadvisor"),
    ("https://shop.abc.com.tw/x", "abc"),
    ("https://shop.example.co.jp/", "example"),
])
def test_source_sites(url, site):
    assert kbn.source_site(url)[0] == site


def test_government_hosts():
    assert kbn.is_gov_host(kbn.source_site("https://www.post.gov.tw/post/a")[1])
    assert kbn.is_gov_host(kbn.source_site("https://zzdo.gov.taipei/x")[1])
    assert not kbn.is_gov_host("gov.tw.example.com") and not kbn.is_gov_host(None)


@pytest.mark.parametrize("url", [
    "https://maps.app.goo.gl/abc", "https://goo.gl/x", "https://bit.ly/x", "https://reurl.cc/x", "https://fb.me/x",
    "https://lin.ee/x", "https://instagr.am/p/x", "https://www.google.com/search?q=x", "https://www.bing.com/search?q=x",
    "https://www.google.com/maps/search/x", "https://web.archive.org/web/1/x", "https://archive.li/x",
    "https://r.jina.ai/https://x.com", "https://abc-com.cdn.ampproject.org/c/x", "https://abc-com.translate.goog/x",
    "https://xn--fiq.com/", "https://user:pw@example.com/x", "ftp://abc.com/x", "https://1.2.3.4/x", "not a url",
])
def test_rejected_sources(url):
    assert kbn.source_site(url)[0] is None


ADDRESS = "台北市中正區重慶南路1段122號"


def supports(html, name="甲麵館", phone=None, content_type="text/html", address=ADDRESS):
    return kbn.page_supports(kbn.page_blocks(html, content_type), name, address=address, phone=phone)


def test_support_field_order():
    # address_city 加在 address_full 後面；原有欄位的意思不變
    assert kbn.Support._fields == ("mentioned", "address", "address_full", "address_city", "phone")


def test_heading_and_next_paragraph_bind():
    assert supports("<h2>甲麵館</h2><p>地址：台北市中正區重慶南路一段122號 電話：02-2345-6789</p>",
                    phone="0223456789") == (True, True, True, True, True)


def test_table_cells_bind_address_and_phone():
    page = "<tr><td>甲麵館</td><td>台北市中正區重慶南路1段122號</td><td>02-2345-6789</td></tr>"
    assert supports(page, phone="0223456789") == (True, True, True, True, True)


@pytest.mark.parametrize("page", [
    "<h2>甲麵館</h2><p>好吃的牛肉麵。</p><h2>乙麵館</h2><p>地址：台北市中正區重慶南路一段122號</p>",
    "<p>甲麵館很好吃。乙麵館 地址：台北市中正區重慶南路1段122號 電話：02-2345-6789</p>",
    "<div>甲麵館\n推薦牛肉麵\n乙麵館 地址：台北市中正區重慶南路1段122號</div>",
    "<p>甲麵館（已歇業）</p><p>乙麵館 地址：台北市中正區重慶南路1段122號</p>",
    "<li>甲麵館</li><li>乙麵館 地址：台北市中正區重慶南路1段122號 電話：02-2345-6789</li>",
    "<p>大甲麵館 地址：台北市中正區重慶南路1段122號 電話：02-2345-6789</p>",
])
def test_other_shops_data_never_binds(page):
    support = supports(page, phone="0223456789")
    assert not support.address and not support.phone


def test_anti_fraud_near_the_name_never_backs_a_phone():
    # 店名前面不是漢字（「！」），店名邊界擋不到；中間也只有「電話」這種連接字，要靠同一段的反詐騙字樣擋（審查員 4）
    page = "<p>注意詐騙！甲麵館 台北市中正區重慶南路1段122號 電話 02-2345-6789</p>"
    support = supports(page, phone="0223456789")
    assert support.mentioned and support.address and not support.phone
    assert supports(page.replace("注意詐騙！", ""), phone="0223456789").phone


def test_closed_shop_paragraph_never_binds():
    assert not supports("<p>已歇業的 甲麵館 台北市中正區重慶南路1段122號</p>").address
    assert not supports("<p>甲麵館 已結束營業 台北市中正區重慶南路1段122號</p>").address


@pytest.mark.parametrize("page, address_ok", [
    ("<p>甲麵館 電話 02-2345-6789 地址 台北市中正區重慶南路1段122號</p>", True),
    ("<p>甲麵館 0223456789 台北市中正區重慶南路1段122號</p>", True),
    ("<p>甲麵館 02-9999-8888 台北市中正區重慶南路1段122號</p>", False),
    ("<p>甲麵館這家店的地址是台北市中正區重慶南路1段122號</p>", True),
])
def test_numbers_between_name_and_address(page, address_ok):
    # 自家電話夾在中間照樣綁得上；別家的號碼（含破折號寫法）就不算（審查員 4）
    assert supports(page, phone="0223456789").address is address_ok


def test_anti_fraud_footer_does_not_spoil_the_page():
    page = ("<h2>甲麵館</h2><p>台北市中正區重慶南路1段122號 電話：02-2345-6789</p>"
            "<footer>防範詐騙：本店不會要求轉帳</footer>")
    assert supports(page, phone="0223456789").phone is True


def test_address_without_district_binds_but_is_not_full():
    support = supports("<h2>甲麵館</h2><p>地址：重慶南路一段122號</p>")
    assert support[1:3] == (True, False) and support.address_city is False


def test_scripts_and_styles_are_not_page_text():
    assert supports("<script>甲麵館 台北市中正區重慶南路1段122號</script><p>別的內容</p>")[:2] == (False, False)


def test_plain_text_pages_split_by_line():
    assert supports("甲麵館\n台北市中正區重慶南路1段122號\n", content_type="text/plain")[1] is True


# ── 最終版審查（審查員 1、3）實測的反例 ───────────────────────────────────────────


@pytest.mark.parametrize("page", [
    "<p>推薦 甲味小館，地址 台北市中正區重慶南路一段122號</p>",
    "<p>這家 甲味小館 地址：台北市中正區重慶南路一段122號</p>",
    "<p>首頁 > 餐廳 甲味小館 台北市中正區重慶南路一段122號</p>",
    "<p>推薦　甲味小館（台北市中正區重慶南路一段122號）</p>",  # 全形空白
])
def test_whitespace_before_the_name_is_a_boundary(page):
    support = supports(page, name="甲味小館")
    assert support.mentioned and support.address and support.address_city


@pytest.mark.parametrize("page", [
    "<p>大甲麵館 地址：台北市中正區重慶南路1段122號</p>",
    "<p>推薦大甲麵館，地址：台北市中正區重慶南路1段122號</p>",
    "<p>大 甲 麵 館 地址：台北市中正區重慶南路1段122號</p>",  # 逐字隔開的標題：空白不算邊界
])
def test_glued_prefix_is_still_not_a_mention(page):
    assert not supports(page).mentioned


def test_address_matching_still_ignores_whitespace():
    page = "<p>甲麵館 地址：台 北 市 中 正 區 重 慶 南 路 一 段 1 2 2 號</p>"
    assert supports(page)[:4] == (True, True, True, True)


@pytest.mark.parametrize("page, name", [
    ("<p>甲味小館 星巴克 地址：台北市中正區重慶南路1段122號 電話：02-2345-6789</p>", "甲味小館"),
    ("<p>甲麵館 延吉 地址：台北市中正區重慶南路1段122號 電話：02-2345-6789</p>", "甲麵館"),
    ("<p>甲麵館二店 地址：台北市中正區重慶南路1段122號 電話：02-2345-6789</p>", "甲麵館"),
    ("<p>甲麵館 Kitchen 地址：台北市中正區重慶南路1段122號 電話：02-2345-6789</p>", "甲麵館"),
    ("<p>甲麵館 2 地址：台北市中正區重慶南路1段122號 電話：02-2345-6789</p>", "甲麵館"),
    ("<p>甲麵館 舊址：台北市中正區重慶南路1段122號 電話：02-2345-6789</p>", "甲麵館"),
    ("<p>甲麵館 隔壁 台北市中正區重慶南路1段122號 電話：02-2345-6789</p>", "甲麵館"),
])
def test_only_connectors_may_sit_between_the_name_and_its_data(page, name):
    # 店名後面到地址（電話）之間只准有標點和「地址、電話、位於」這類連接字；別家店名、分店、英文、數字都不算
    support = supports(page, name=name, phone="0223456789")
    assert support.mentioned and not support.address and not support.phone


CHAIN = "台北市信義區甲乙路100號"
MOBILE = dict(phone="0900000000")


@pytest.mark.parametrize("page, phone_ok, kw", [
    ("<p>甲麵館（台北市大安區復興南路一段100號）</p>", False, dict(address="台北市大安區復興南路1段100號")),
    ("<p>甲麵館｜地址：台北市中正區重慶南路1段122號｜電話：02-2345-6789</p>", True, {}),
    ("<p>甲麵館 電話：02-2345-6789 地址：台北市中正區重慶南路1段122號</p>", True, {}),
    ("<p>甲麵館位於台北市中正區重慶南路一段122號</p>", False, {}),
    ("<p>甲麵館，地址：台北市中正區重慶南路1段122號，電話：02-2345-6789</p>", True, {}),
    ("<p>【甲麵館】台北市中正區重慶南路1段122號 TEL：02-2345-6789</p>", True, {}),
    ("<div>甲麵館\n地址：台北市中正區重慶南路1段122號\n電話：02-2345-6789</div>", True, {}),
    ("<h3>甲麵館</h3><ul><li>地址：台北市中正區重慶南路1段122號</li><li>電話：02-2345-6789</li></ul>", True, {}),
    ("<p>甲麵館 地址：100台北市中正區重慶南路1段122號3樓 聯絡電話：02-2345-6789</p>", True, {}),
    ("<p>甲麵館的地址是台北市中正區重慶南路1段122號，電話是02-2345-6789</p>", True, {}),
    # 第二輪審查：更多標籤（只收標籤，不收說明文字）
    ("<p>甲麵館 地點：台北市中正區重慶南路1段122號 電話：02-2345-6789</p>", True, {}),
    ("<p>甲麵館 位置：台北市中正區重慶南路1段122號</p>", False, {}),
    ("<p>甲麵館 店家地址：台北市中正區重慶南路1段122號 店家電話：02-2345-6789</p>", True, {}),
    ("<p>甲麵館 門市地址：台北市中正區重慶南路1段122號 門市電話：02-2345-6789</p>", True, {}),
    ("<p>甲麵館 店家位置 台北市中正區重慶南路1段122號 電話號碼 02-2345-6789</p>", True, {}),
    ("<p>甲麵館 Address: 台北市中正區重慶南路1段122號 Phone: 02-2345-6789</p>", True, {}),
    ("<p>甲麵館 ADDR 台北市中正區重慶南路1段122號 Telephone 02-2345-6789</p>", True, {}),
    ("<p>甲麵館 地址：台北市中正區重慶南路1段122號 訂位專線：02-2345-6789</p>", True, {}),
    ("<p>甲麵館 地址：台北市中正區重慶南路1段122號 預約電話：02-2345-6789</p>", True, {}),
    ("<p>甲麵館 地址：台北市中正區重慶南路1段122號 專線 02-2345-6789</p>", True, {}),
    ("<p>甲麵館 地址：台北市中正區重慶南路1段122號 市話：02-2345-6789</p>", True, {}),
    ("<p>甲麵館 地址：台北市中正區重慶南路1段122號 手機：0900-000-000</p>", True, MOBILE),
    # 電話在前、地址在後，兩個都要綁得上
    ("<p>甲麵館 店家電話：02-2345-6789 店家地址：台北市中正區重慶南路1段122號</p>", True, {}),
    ("<p>甲麵館 訂位專線 02-2345-6789 地點 台北市中正區重慶南路1段122號</p>", True, {}),
    ("<p>甲麵館 Phone: 02-2345-6789 Address: 台北市中正區重慶南路1段122號</p>", True, {}),
    ("<p>甲麵館 手機 0900-000-000 位置 台北市中正區重慶南路1段122號</p>", True, MOBILE),
    ("<p>甲麵館 聯絡電話號碼：02-2345-6789 門市地址：台北市中正區重慶南路1段122號</p>", True, {}),
    # 括號裡的郵遞區號
    ("<p>甲麵館 地址：(100)台北市中正區重慶南路1段122號</p>", False, {}),
    ("<p>甲麵館 地址：（100）台北市中正區重慶南路1段122號 電話：02-2345-6789</p>", True, {}),
    ("<p>甲麵館 地址：[100]台北市中正區重慶南路1段122號</p>", False, {}),
    ("<p>甲麵館 電話 02-2345-6789 地址 (100)台北市中正區重慶南路1段122號</p>", True, {}),
    # 電話後面直接接括號郵遞區號：找電話的程式把「6789 (100」讀成一串 13 碼，電話照舊綁不上（只少收）
    ("<p>甲麵館 電話 02-2345-6789 (100)台北市中正區重慶南路1段122號</p>", False, {}),
    # 連鎖店的分店寫在括號裡、或和品牌隔一個空白
    ("<p>鼎泰豐（信義店） 地址：台北市信義區甲乙路100號 電話：02-2345-6789</p>", True,
     dict(name="鼎泰豐信義店", address=CHAIN)),
    ("<p>鼎泰豐信義店 地址：台北市信義區甲乙路100號</p>", False, dict(name="鼎泰豐(信義店)", address=CHAIN)),
    ("<p>推薦 鼎泰豐 信義店，地址：台北市信義區甲乙路100號</p>", False, dict(name="鼎泰豐信義店", address=CHAIN)),
    ("<p>推薦 鼎泰豐 (信義店) 地址：台北市信義區甲乙路100號</p>", False, dict(name="鼎泰豐 信義店", address=CHAIN)),
    ("<p>鼎泰豐 信義店 地址：台北市信義區甲乙路100號</p>", False, dict(name="鼎泰豐（信義店）", address=CHAIN)),
])
def test_legit_layouts_still_bind(page, phone_ok, kw):
    support = supports(page, **{"address": ADDRESS, "phone": "0223456789", **kw})
    assert support.mentioned and support.address and support.address_full and support.address_city
    assert support.phone is phone_ok


@pytest.mark.parametrize("page, address_ok", [
    ("<p>甲麵館 地址：台北市中正區重慶南路1段122號 營業時間：11:00-21:00 電話：02-2345-6789</p>", True),
    ("<p>甲麵館 地址：台北市中正區重慶南路1段122號（近捷運甲乙站） 電話：02-2345-6789</p>", True),
    ("<p>甲麵館 地址：台北市中正區重慶南路1段122號 ★4.5（120則評論） 電話：02-2345-6789</p>", True),
    ("<p>甲麵館 ★4.5（120則評論） 地址：台北市中正區重慶南路1段122號 電話：02-2345-6789</p>", False),
    ("<p>甲麵館 地址：台北市中正區重慶南路1段122號 乙麵館 電話：02-2345-6789</p>", True),
    ("<p>甲麵館 營業時間 11:00-21:00 電話：02-2345-6789 地址：台北市中正區重慶南路1段122號</p>", False),
])
def test_notes_and_neighbours_still_block(page, address_ok):
    # 刻意保留的精確度：營業時間、評分、括號說明、別家店名夾在中間，後面的電話（地址）就不算
    support = supports(page, phone="0223456789")
    assert support.address is address_ok and not support.phone


@pytest.mark.parametrize("label", ["傳真：", "FAX：", "TEL/FAX：", "客服電話：", "客服專線 ", "總機："])
def test_fax_and_hotline_labels_never_back_the_phone(label):
    # 傳真號碼不能變成電話；客服、總機可能是總公司。「TEL/FAX」寫在一起的也不收（照舊綁不上）
    support = supports(f"<p>甲麵館 地址：台北市中正區重慶南路1段122號 {label}02-2345-6789</p>", phone="0223456789")
    assert support.address and not support.phone


def test_postal_code_never_swallows_a_phone_tail():
    # 郵遞區號不能吃掉前面電話的尾巴：沒有括號的要前面不是數字、「之」「-」；有括號的整組在括號裡
    text = kbn.address_text("電話0900-000-000台北市中正區重慶南路1段122號")
    [span] = kbn.address_spans(text, FULL)
    assert text[span.start:span.end] == "台北市中正區重慶南路1段122號"
    text = kbn.address_text("電話(02)2345-6789(100)台北市中正區重慶南路1段122號")
    [span] = kbn.address_spans(text, FULL)
    assert text[span.start:span.end] == "(100)台北市中正區重慶南路1段122號"
    support = supports("<p>甲麵館 電話：0900-000-000台北市中正區重慶南路1段122號</p>", **MOBILE)
    assert support.address and support.phone
    assert not supports("<p>甲麵館 電話：0900-000-000台北市中正區重慶南路1段122號</p>", phone="0223456789").address


@pytest.mark.parametrize("name", ["鼎泰豐信義店", "鼎泰豐 信義店", "鼎泰豐(信義店)", "鼎泰豐（信義店）"])
def test_chain_branch_spellings(name):
    # 品牌和分店之間可以什麼都沒有、一個空白或一對括號；店名寫哪一種都一樣
    tail = " 地址：台北市信義區甲乙路100號</p>"
    for written in ("鼎泰豐信義店", "鼎泰豐 信義店", "鼎泰豐（信義店）", "鼎泰豐 (信義店)", "鼎泰豐( 信義店 )"):
        assert supports("<p>" + written + tail, name=name, address=CHAIN).address, written
    for other in ("鼎泰豐（大安店）", "鼎泰豐大安店", "鼎泰豐（信義店旁）", "推薦鼎泰豐（信義店）", "推薦 鼎 泰 豐 信 義 店"):
        assert not supports("<p>" + other + tail, name=name, address=CHAIN).mentioned, other


def test_latin_names_need_a_right_boundary():
    tail = " 地址：台北市中正區重慶南路1段122號</p>"
    assert not supports("<p>Mosaic Cafeteria" + tail, name="Mosaic Cafe").mentioned
    assert not supports("<p>Cafe 1011" + tail, name="Cafe 101").mentioned
    assert supports("<p>Mosaic Cafe" + tail, name="Mosaic Cafe").address
    assert supports("<p>Mosaic Cafe（台北市中正區重慶南路1段122號）</p>", name="Mosaic Cafe").address


def test_space_after_a_latin_word_is_not_a_boundary_for_latin_names():
    # 英文店名本來就用空白隔字：「Big Mosaic Cafe」是別家，不能因為空白就算提到「Mosaic Cafe」
    tail = " 地址：台北市中正區重慶南路1段122號</p>"
    assert not supports("<p>Big Mosaic Cafe" + tail, name="Mosaic Cafe").mentioned
    assert supports("<p>推薦 Mosaic Cafe" + tail, name="Mosaic Cafe").address
    assert supports("<p>Menu: Mosaic Cafe" + tail, name="Mosaic Cafe").address


@pytest.mark.parametrize("full", ["台北市中正區中正路100號", "基隆市中正區中正路100號"])
def test_district_only_page_does_not_confirm_the_city(full):
    # 中正區台北、基隆都有：只寫區的頁核對不了縣市（最終版審查員 1）
    support = supports("<p>甲小館 地址：中正區中正路100號</p>", name="甲小館", address=full)
    assert support.address and support.address_full and not support.address_city


def test_full_address_confirms_the_city():
    page = "<p>甲小館 地址：台北市中正區中正路100號</p>"
    support = supports(page, name="甲小館", address="台北市中正區中正路100號")
    assert support.address and support.address_full and support.address_city
    assert not supports(page, name="甲小館", address="基隆市中正區中正路100號").address


def test_bracket_spellings_share_one_key():
    # 第一輪寫「鼎泰豐(信義店)」、第二輪寫「鼎泰豐信義店」也要是同一家
    keys = {kbn.name_key(n) for n in ("鼎泰豐(信義店)", "鼎泰豐（信義店）", "鼎泰豐信義店", "鼎泰豐 信義店")}
    assert keys == {"鼎泰豐信義店"}
    assert kbn.name_key("鼎泰豐(大安店)") != kbn.name_key("鼎泰豐(信義店)")


@pytest.mark.parametrize("name", ["鼎泰豐(信義店", "鼎泰豐信義店)", "甲麵館)(", "甲麵館(大安)(二店)", "甲麵館()",
                                  "鼎泰豐(總)店", "鼎(泰豐)信義店", "(甲麵館)", "全家(福)", "甲麵館(大安)",
                                  "甲麵館〔大安〕", "甲麵館《二店》", "甲麵館〈二店〉", "甲麵館『二店』"])
def test_unbalanced_or_repeated_brackets_are_rejected(name):
    assert kbn.validate_name(name, "餐廳")[0] is None
