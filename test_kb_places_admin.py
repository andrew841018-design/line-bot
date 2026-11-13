"""jobs/kb_places_admin：知識庫唯一的寫入口與兩輪查證（2026-10-31，第一批段一）。

不連網：`_fetch` 換成假的，網頁內容都是合成的；時間用 monkeypatch 換 `_now`、狀態資料夾換
`DEFAULT_STATE_DIR`（正式指令沒有 --now、--state-dir）。狀態檔放 tmp_path，不碰正式 state/。含實作審查（審查員 1、3）實測過的反例。
"""
from __future__ import annotations

import json
import stat
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "jobs"))
import kb_places_admin as admin  # noqa: E402

GROUP = "C" + "a" * 32
OTHER_GROUP = "C" + "b" * 32
NOW = 1_793_400_000  # 2026-10-31 台北時間
LATER = NOW + 13 * 3600
DAY = 86400
NAME = "甲麵館"
ADDRESS = "台北市中正區重慶南路一段122號"
PHONE = "02-2345-6789"


def page(address=ADDRESS, phone=PHONE, name=NAME):
    return f"<h2>{name}</h2><p>地址：{address} 電話：{phone}</p>"


GOOD = page()
NO_PHONE = f"<h2>{NAME}</h2><p>地址：{ADDRESS}</p>"
OTHER_ADDRESS = page(address="台北市中正區重慶南路一段200號")
OTHER_PHONE = page(phone="02-2345-0000")
LISTICLE = f"<h2>{NAME}</h2><p>好吃。</p><h2>乙麵館</h2><p>地址：{ADDRESS} 電話：{PHONE}</p>"
FRAUD = f"<p>小心詐騙！有人假冒{NAME} {ADDRESS} 來電 {PHONE}</p>"


@pytest.fixture
def pages(monkeypatch):
    """網址 → html，或 網址 → (轉址後的網址, html)。沒列的網址當成抓不到。"""
    table: dict = {}

    def fake_fetch(url):
        if url not in table:
            raise ConnectionError("unreachable")
        value = table[url]
        final, body = value if isinstance(value, tuple) else (url, value)
        return final, admin.kbn.page_blocks(body)

    monkeypatch.setattr(admin, "_fetch", fake_fetch)
    return table


@pytest.fixture
def run(monkeypatch):
    def _run(state, *argv, now=NOW):
        monkeypatch.setattr(admin, "_now", lambda: now)
        monkeypatch.setattr(admin, "DEFAULT_STATE_DIR", state)
        return admin.main(list(argv))

    return _run


def place_args(*sources, address=ADDRESS, phone=PHONE, kind="餐廳", name=NAME, nominate=f"{GROUP}:123456"):
    args = ["--name", name, "--kind", kind, "--address", address]
    if phone:
        args += ["--phone", phone]
    for url in sources:
        args += ["--source", url]
    if nominate:
        args += ["--nominate", nominate]
    return args


def data(state):
    return json.loads((state / admin.DATA_NAME).read_text(encoding="utf-8"))


def events(state):
    return [json.loads(line) for line in (state / admin.EVENTS_NAME).read_text(encoding="utf-8").splitlines()]


ROUND1 = ("https://a1.com/x", "https://a2.com/x")
ROUND2 = ("https://b1.com/x", "https://b2.com/x")


def two_rounds(state, pages, run, first=GOOD, second=GOOD, start=NOW, **kw):
    for url in ROUND1:
        pages[url] = first
    for url in ROUND2:
        pages[url] = second
    assert run(state, "propose", *place_args(*ROUND1, **kw), now=start) == 0
    return run(state, "confirm", *place_args(*ROUND2, **kw), now=start + 13 * 3600)


def test_first_round_stores_hashes_only(tmp_path, pages, run):
    pages.update({u: GOOD for u in ROUND1})
    assert run(tmp_path, "propose", *place_args(*ROUND1)) == 0
    text = (tmp_path / admin.DATA_NAME).read_text(encoding="utf-8")
    log = (tmp_path / admin.EVENTS_NAME).read_text(encoding="utf-8")
    for secret in ("重慶南路", "0223456789", "a1.com", "中正區"):
        assert secret not in text and secret not in log
    assert data(tmp_path)["places"] == []


def test_two_matching_rounds_activate(tmp_path, pages, run):
    assert two_rounds(tmp_path, pages, run) == 0
    [place] = data(tmp_path)["places"]
    assert place["status"] == "active" and place["phone"] == "0223456789"
    assert place["address"] == "台北市中正區重慶南路1段122號" and place["region"] == "台北市中正區"
    assert place["verified_on"] == "2026-10-31" and place["nominations"] == [[GROUP, "123456"]]
    assert data(tmp_path)["pending"] == []
    assert stat.S_IMODE((tmp_path / admin.DATA_NAME).stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / admin.EVENTS_NAME).stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".kb_places.*.tmp"))


def test_second_round_too_soon_then_same_site_discards(tmp_path, pages, run):
    pages.update({u: GOOD for u in ROUND1 + ROUND2})
    pages["https://www.a2.com/y"] = GOOD
    assert run(tmp_path, "propose", *place_args(*ROUND1)) == 0
    assert run(tmp_path, "confirm", *place_args("https://b1.com/x", "https://www.a2.com/y"), now=NOW + 3600) == 10
    assert len(data(tmp_path)["pending"]) == 1
    assert run(tmp_path, "confirm", *place_args("https://b1.com/x", "https://www.a2.com/y"), now=LATER) == 5
    assert data(tmp_path)["pending"] == [] and data(tmp_path)["places"] == []
    assert run(tmp_path, "confirm", *place_args(*ROUND2), now=LATER + 60) == 10


def test_confirm_without_first_round(tmp_path, pages, run):
    pages.update({u: GOOD for u in ROUND2})
    assert run(tmp_path, "confirm", *place_args(*ROUND2), now=LATER) == 10


def test_first_round_is_not_restarted_while_pending(tmp_path, pages, run):
    pages.update({u: GOOD for u in ROUND1})
    assert run(tmp_path, "propose", *place_args(*ROUND1)) == 0
    assert run(tmp_path, "propose", *place_args(*ROUND1), now=NOW + DAY) == 10
    assert data(tmp_path)["pending"][0]["proposed_at"] == NOW
    assert run(tmp_path, "propose", *place_args(*ROUND1), now=NOW + 8 * DAY) == 0


def test_expired_first_round_cannot_be_confirmed(tmp_path, pages, run):
    pages.update({u: GOOD for u in ROUND1 + ROUND2})
    assert run(tmp_path, "propose", *place_args(*ROUND1)) == 0
    assert run(tmp_path, "confirm", *place_args(*ROUND2), now=NOW + 8 * DAY) == 10
    assert data(tmp_path)["pending"] == []


def test_different_address_same_region_is_a_mismatch(tmp_path, pages, run):
    pages.update({u: GOOD for u in ROUND1})
    pages.update({u: OTHER_ADDRESS for u in ROUND2})
    assert run(tmp_path, "propose", *place_args(*ROUND1)) == 0
    other = place_args(*ROUND2, address="台北市中正區重慶南路一段200號")
    assert run(tmp_path, "confirm", *other, now=LATER) == 5
    assert data(tmp_path)["places"] == [] and data(tmp_path)["pending"] == []


def test_kind_may_differ_between_rounds(tmp_path, pages, run):
    pages.update({u: GOOD for u in ROUND1 + ROUND2})
    assert run(tmp_path, "propose", *place_args(*ROUND1, kind="餐廳")) == 0
    assert run(tmp_path, "confirm", *place_args(*ROUND2, kind="店家"), now=LATER) == 0


def test_phone_kept_only_when_bound(tmp_path, pages, run):
    assert two_rounds(tmp_path, pages, run, first=NO_PHONE, second=NO_PHONE) == 0
    [place] = data(tmp_path)["places"]
    assert place["phone"] == "" and place["phone_hash"] == "" and place["phone_source"] == ""


def test_anti_fraud_never_backs_a_phone(tmp_path, pages, run):
    pages.update({u: FRAUD for u in ROUND1})
    assert run(tmp_path, "propose", *place_args(*ROUND1)) == 7  # 「假冒甲麵館」：店名前面緊接漢字，不算提到
    # 警語放在電話後面：店名和地址中間只准連接詞（10/31 第 2 輪起），警語夾在中間會連地址都不綁
    assert two_rounds(tmp_path / "b", pages, run, first=GOOD.replace("</p>", " 小心詐騙</p>"), second=GOOD) == 0
    assert data(tmp_path / "b")["places"][0]["phone"] == ""


def test_listicle_page_does_not_verify(tmp_path, pages, run):
    pages.update({u: LISTICLE for u in ROUND1})
    assert run(tmp_path, "propose", *place_args(*ROUND1)) == 7
    assert not (tmp_path / admin.DATA_NAME).exists()


def test_unfetchable_or_same_brand_sources_do_not_count(tmp_path, pages, run):
    pages["https://a1.com/x"] = GOOD
    assert run(tmp_path, "propose", *place_args("https://a1.com/x", "https://made-up.com/x")) == 7
    pages.update({"https://www.google.com/maps/place/a": GOOD, "https://www.google.com.tw/maps/place/b": GOOD})
    assert run(tmp_path, "propose", *place_args("https://www.google.com/maps/place/a",
                                                "https://www.google.com.tw/maps/place/b")) == 7
    pages.update({"https://a3.com/x": ("https://a1.com/y", GOOD)})
    assert run(tmp_path, "propose", *place_args("https://a1.com/x", "https://a3.com/x")) == 7  # 轉址到同一站
    pages.update({"https://a4.com/x": ("https://bit.ly/z", GOOD)})
    assert run(tmp_path, "propose", *place_args("https://a1.com/x", "https://a4.com/x")) == 10


@pytest.mark.parametrize("argv", [
    place_args(*ROUND1, name="鼎泰豐"),
    place_args(*ROUND1, name="甲牙醫診所"),
    place_args(*ROUND1, name="台北北門郵局", kind="店家"),
    place_args(*ROUND1, address="中山路3号"),
    place_args(*ROUND1, phone="2345-6789"),
    place_args("https://a1.com/x"),
    place_args("https://a1.com/x", "https://bit.ly/x"),
    place_args("https://a1.com/x", "https://a2.com/search?q=0223456789"),
    place_args(*ROUND1, nominate="group:1"),
])
def test_bad_input_is_rejected(tmp_path, pages, run, argv):
    pages.update({u: GOOD for u in ROUND1})
    pages["https://a2.com/search?q=0223456789"] = GOOD
    assert run(tmp_path, "propose", *argv) == 10


@pytest.mark.parametrize("argv", [["--now", str(LATER), "stats"], ["--state-dir", "/tmp/x", "stats"]])
def test_no_now_or_state_dir_flags_in_production(argv):
    with pytest.raises(SystemExit):
        admin.main(argv)


def test_offices_need_a_gov_page_with_the_address(tmp_path, pages, run):
    office = "台北北門郵局"
    body = f"<h2>{office}</h2><p>{ADDRESS}</p>"
    pages.update({"https://a1.com/x": body, "https://a2.com/x": body, "https://www.post.gov.tw/x": body})
    base = dict(kind="郵局", name=office, phone=None)
    assert run(tmp_path, "propose", *place_args(*ROUND1, **base)) == 7
    assert run(tmp_path, "propose", *place_args("https://a1.com/x", "https://www.post.gov.tw/nope", **base)) == 7
    assert run(tmp_path, "propose", *place_args("https://a1.com/x", "https://www.post.gov.tw/x", **base)) == 0


def test_daily_cap(tmp_path, pages, run, monkeypatch):
    monkeypatch.setattr(admin, "DAILY_CAP", 2)
    pages.update({u: GOOD for u in ROUND1})
    assert run(tmp_path, "propose", *place_args(*ROUND1)) == 0
    assert run(tmp_path, "propose", *place_args(*ROUND1, name="乙麵館")) == 7  # 頁上沒有乙麵館：不寫入、不計數
    pages.update({u: page(name="乙麵館") for u in ("https://c1.com/x", "https://c2.com/x")})
    assert run(tmp_path, "propose", *place_args("https://c1.com/x", "https://c2.com/x", name="乙麵館")) == 0
    pages.update({u: page(name="丙麵館") for u in ("https://d1.com/x", "https://d2.com/x")})
    assert run(tmp_path, "propose", *place_args("https://d1.com/x", "https://d2.com/x", name="丙麵館")) == 4


def test_disabled_blocks_writes_but_not_revert(tmp_path, pages, run):
    assert two_rounds(tmp_path, pages, run) == 0
    (tmp_path / admin.DISABLED_NAME).write_text("", encoding="utf-8")
    pages.update({u: GOOD for u in ("https://c1.com/x", "https://c2.com/x")})
    assert run(tmp_path, "propose", *place_args("https://c1.com/x", "https://c2.com/x"), now=LATER + 60) == 3
    activation = next(e for e in events(tmp_path) if e["action"] == "activate")
    assert run(tmp_path, "revert", "--event", activation["id"], now=LATER + 120) == 0
    assert data(tmp_path)["places"] == []
    # 停用時 stats、todo 印完回 3：查證排程第 1 步就停（審查員 1、4）
    assert run(tmp_path, "stats", now=LATER + 150) == 3 and run(tmp_path, "todo", now=LATER + 150) == 3
    assert run(tmp_path, "revert", "--event", activation["id"], now=LATER + 180) == 10


def test_revert_refuses_when_state_moved_on(tmp_path, pages, run):
    assert two_rounds(tmp_path, pages, run) == 0
    first_activation = next(e for e in events(tmp_path) if e["action"] == "activate")
    assert two_rounds(tmp_path, pages, run, start=NOW + 101 * DAY) == 0  # 重查：同一筆更新了
    assert run(tmp_path, "revert", "--event", first_activation["id"], now=NOW + 102 * DAY) == 10


def test_bad_file_is_never_overwritten(tmp_path, pages, run):
    (tmp_path / admin.DATA_NAME).write_text('{"version": 1, "places": [{"name": "x"}], "pending": []}', encoding="utf-8")
    before = (tmp_path / admin.DATA_NAME).read_text(encoding="utf-8")
    pages.update({u: GOOD for u in ROUND1})
    assert run(tmp_path, "propose", *place_args(*ROUND1)) == 6
    assert run(tmp_path, "stats") == 6 and run(tmp_path, "todo") == 6
    assert (tmp_path / admin.DATA_NAME).read_text(encoding="utf-8") == before


def test_changed_phone_suspends_then_old_phone_confirmed(tmp_path, pages, run):
    assert two_rounds(tmp_path, pages, run) == 0
    later = NOW + 200 * DAY
    pages.update({u: OTHER_PHONE for u in ("https://c1.com/x", "https://c2.com/x")})
    pages.update({u: GOOD for u in ("https://d1.com/x", "https://d2.com/x")})
    assert run(tmp_path, "propose", *place_args("https://c1.com/x", "https://c2.com/x", phone="02-2345-0000"), now=later) == 0
    assert data(tmp_path)["places"][0]["status"] == "suspended"
    # 地址兩輪一致；電話第一輪不同，第二輪和已收的一樣 → 收進，留已收的電話，查證日期更新
    assert run(tmp_path, "confirm", *place_args("https://d1.com/x", "https://d2.com/x"), now=later + 13 * 3600) == 0
    [place] = data(tmp_path)["places"]
    assert place["status"] == "active" and place["phone"] == "0223456789" and place["verified_on"] != "2026-10-31"


def test_restore_when_second_round_matches_old_address(tmp_path, pages, run):
    assert two_rounds(tmp_path, pages, run) == 0
    later = NOW + 200 * DAY
    moved = "台北市中正區重慶南路一段200號"
    pages.update({u: OTHER_ADDRESS for u in ("https://c1.com/x", "https://c2.com/x")})
    pages.update({u: GOOD for u in ("https://d1.com/x", "https://d2.com/x")})
    assert run(tmp_path, "propose", *place_args("https://c1.com/x", "https://c2.com/x", address=moved), now=later) == 0
    assert data(tmp_path)["places"][0]["status"] == "suspended"
    assert run(tmp_path, "confirm", *place_args("https://d1.com/x", "https://d2.com/x"), now=later + 13 * 3600) == 0
    [place] = data(tmp_path)["places"]
    assert place["status"] == "active" and place["verified_on"] == "2026-10-31" and place["phone"] == "0223456789"


def test_restore_drops_a_disputed_phone(tmp_path, pages, run):
    assert two_rounds(tmp_path, pages, run) == 0
    later = NOW + 200 * DAY
    moved = "台北市中正區重慶南路一段200號"
    pages.update({u: OTHER_ADDRESS for u in ("https://c1.com/x", "https://c2.com/x")})
    pages.update({u: OTHER_PHONE for u in ("https://d1.com/x", "https://d2.com/x")})
    assert run(tmp_path, "propose", *place_args("https://c1.com/x", "https://c2.com/x", address=moved), now=later) == 0
    assert run(tmp_path, "confirm", *place_args("https://d1.com/x", "https://d2.com/x", phone="02-2345-0000"),
               now=later + 13 * 3600) == 0
    [place] = data(tmp_path)["places"]
    assert place["status"] == "active" and place["phone"] == ""


def test_recheck_without_phone_does_not_suspend(tmp_path, pages, run):
    assert two_rounds(tmp_path, pages, run) == 0
    later = NOW + 101 * DAY
    pages.update({u: NO_PHONE for u in ("https://c1.com/x", "https://c2.com/x")})
    assert run(tmp_path, "propose", *place_args("https://c1.com/x", "https://c2.com/x"), now=later) == 0
    assert data(tmp_path)["places"][0]["status"] == "active"
    pages.update({u: GOOD for u in ("https://d1.com/x", "https://d2.com/x")})
    assert run(tmp_path, "confirm", *place_args("https://d1.com/x", "https://d2.com/x"), now=later + 13 * 3600) == 0
    [place] = data(tmp_path)["places"]
    assert place["phone"] == "0223456789"  # 和已收的電話相同就留


def test_recheck_mismatch_suspends(tmp_path, pages, run):
    assert two_rounds(tmp_path, pages, run) == 0
    later = NOW + 101 * DAY
    pages.update({u: GOOD for u in ("https://c1.com/x", "https://c2.com/x")})
    pages.update({u: OTHER_ADDRESS for u in ("https://d1.com/x", "https://d2.com/x")})
    assert run(tmp_path, "propose", *place_args("https://c1.com/x", "https://c2.com/x"), now=later) == 0
    other = place_args("https://d1.com/x", "https://d2.com/x", address="台北市中正區重慶南路一段200號")
    assert run(tmp_path, "confirm", *other, now=later + 13 * 3600) == 5
    assert data(tmp_path)["places"][0]["status"] == "suspended"


def test_same_name_in_two_regions_is_kept_apart(tmp_path, pages, run):
    assert two_rounds(tmp_path, pages, run) == 0
    tainan = "台南市中西區民生路一段5號"
    body = page(address=tainan, phone="06-222-3333")
    pages.update({u: body for u in ("https://c1.com/x", "https://c2.com/x", "https://d1.com/x", "https://d2.com/x")})
    kw = dict(address=tainan, phone="06-222-3333", nominate=f"{OTHER_GROUP}:654321")
    assert run(tmp_path, "propose", *place_args("https://c1.com/x", "https://c2.com/x", **kw), now=LATER + 60) == 0
    assert run(tmp_path, "confirm", *place_args("https://d1.com/x", "https://d2.com/x", **kw), now=LATER + 14 * 3600) == 0
    places = {p["region"]: p for p in data(tmp_path)["places"]}
    assert set(places) == {"台北市中正區", "台南市中西區"}
    assert places["台北市中正區"]["nominations"] == [[GROUP, "123456"]]
    assert places["台南市中西區"]["nominations"] == [[OTHER_GROUP, "654321"]]


def test_freshness_is_decided_when_read(tmp_path, pages, run):
    assert two_rounds(tmp_path, pages, run) == 0
    [place] = data(tmp_path)["places"]
    assert admin.is_fresh(place, NOW + 119 * DAY)
    assert not admin.is_fresh(place, NOW + 120 * DAY)


def test_stats_and_todo_print_no_addresses(tmp_path, pages, run, capsys):
    pages.update({u: GOOD for u in ROUND1})
    assert run(tmp_path, "propose", *place_args(*ROUND1)) == 0
    capsys.readouterr()
    assert run(tmp_path, "stats", now=LATER) == 0
    assert run(tmp_path, "todo", "--recheck", now=LATER) == 0
    out = capsys.readouterr().out
    assert "第二輪可以做：甲麵館" in out
    for secret in ("重慶南路", "0223456789", "2345", "a1.com", "中正區"):
        assert secret not in out


def test_real_fetch_refuses_non_2xx(monkeypatch):
    class Resp:
        status_code = 404
        url = "https://a1.com/x"
        headers = {"Content-Type": "text/html"}
        content = GOOD.encode("utf-8")
        encoding = "utf-8"

    monkeypatch.setitem(sys.modules, "safe_fetch", types.SimpleNamespace(fetch=lambda *a, **k: Resp()))
    with pytest.raises(ConnectionError):
        admin._fetch("https://a1.com/x")
    Resp.status_code = 200
    final, blocks = admin._fetch("https://a1.com/x")
    assert final == "https://a1.com/x" and any(NAME in b for b in blocks)


@pytest.mark.parametrize("field, value", [
    ("address", "臺北市中正區重慶南路一段122號"),  # 不是正規化寫法
    ("phone", "1234"),
    ("name", "鼎泰豐"),
    ("region", "台北市大安區"),
    ("verified_on", "2026-13-01"),
    ("status", "maybe"),
    ("sources", ["https://bit.ly/x", "https://a.com/x"]),
    ("nominations", [["group", "1"]]),
])
def test_any_bad_field_rejects_the_whole_file(tmp_path, pages, run, field, value):
    assert two_rounds(tmp_path, pages, run) == 0
    stored = data(tmp_path)
    stored["places"][0][field] = value
    (tmp_path / admin.DATA_NAME).write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    assert run(tmp_path, "stats", now=LATER + 60) == 6


def test_revert_of_a_suspension_restores_the_old_record(tmp_path, pages, run):
    assert two_rounds(tmp_path, pages, run) == 0
    pages.update({u: OTHER_PHONE for u in ("https://c1.com/x", "https://c2.com/x")})
    later = NOW + 30 * DAY
    assert run(tmp_path, "propose", *place_args("https://c1.com/x", "https://c2.com/x", phone="02-2345-0000"), now=later) == 0
    suspension = next(e for e in events(tmp_path) if e["action"] == "suspend")
    assert run(tmp_path, "revert", "--event", suspension["id"], now=later + 60) == 0
    assert data(tmp_path)["places"][0]["status"] == "active"


def test_revert_refuses_a_forged_before(tmp_path, pages, run):
    assert two_rounds(tmp_path, pages, run) == 0
    current = data(tmp_path)["places"][0]
    forged = dict(current, phone="0900000000")
    event = {"id": "f" * 32, "ts": LATER, "action": "activate", "record": current["place_id"],
             "before": forged, "after": current}
    with (tmp_path / admin.EVENTS_NAME).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    assert run(tmp_path, "revert", "--event", "f" * 32, now=LATER + 60) == 10
    assert data(tmp_path)["places"][0]["phone"] == "0223456789"


def test_missing_salt_with_data_is_refused(tmp_path, pages, run):
    assert two_rounds(tmp_path, pages, run) == 0
    (tmp_path / admin.SALT_NAME).unlink()
    pages.update({u: GOOD for u in ("https://c1.com/x", "https://c2.com/x")})
    assert run(tmp_path, "propose", *place_args("https://c1.com/x", "https://c2.com/x"), now=NOW + 101 * DAY) == 6
    assert data(tmp_path)["places"][0]["status"] == "active"


def test_symlinked_state_files_are_refused(tmp_path, pages, run):
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("", encoding="utf-8")
    (tmp_path / admin.EVENTS_NAME).symlink_to(target)
    pages.update({u: GOOD for u in ROUND1})
    assert run(tmp_path, "propose", *place_args(*ROUND1)) == 6
    assert target.read_text(encoding="utf-8") == ""


def test_second_round_in_another_region_discards(tmp_path, pages, run):
    pages.update({u: GOOD for u in ROUND1})
    tainan = "台南市中西區民生路一段5號"
    pages.update({u: page(address=tainan) for u in ROUND2})
    assert run(tmp_path, "propose", *place_args(*ROUND1)) == 0
    assert run(tmp_path, "confirm", *place_args(*ROUND2, address=tainan), now=LATER) == 5
    assert data(tmp_path)["pending"] == []


def test_disabled_while_fetching_stops_the_write(tmp_path, pages, run, monkeypatch):
    real = admin._fetch

    def fetch_then_disable(url):
        (tmp_path / admin.DISABLED_NAME).write_text("", encoding="utf-8")
        return real(url)

    pages.update({u: GOOD for u in ROUND1})
    monkeypatch.setattr(admin, "_fetch", fetch_then_disable)
    assert run(tmp_path, "propose", *place_args(*ROUND1)) == 3
    assert not (tmp_path / admin.DATA_NAME).exists()


def test_redirect_to_a_url_with_the_phone_is_refused(tmp_path, pages, run):
    pages["https://a1.com/x"] = GOOD
    pages["https://a2.com/x"] = ("https://a2.com/search?tel=0223456789", GOOD)
    assert run(tmp_path, "propose", *place_args(*ROUND1)) == 10


def test_too_many_sources_are_refused(tmp_path, pages, run):
    urls = [f"https://s{i}.com/x" for i in range(admin.MAX_SOURCES + 1)]
    pages.update({u: GOOD for u in urls})
    assert run(tmp_path, "propose", *place_args(*urls)) == 10


def test_gov_page_must_bind_the_address(tmp_path, pages, run):
    office = "台北北門郵局"
    good = f"<h2>{office}</h2><p>{ADDRESS}</p>"
    vague = f"<h2>{office}</h2><p>服務時間請洽各局</p>"
    pages.update({"https://a1.com/x": good, "https://www.post.gov.tw/x": vague})
    base = dict(kind="郵局", name=office, phone=None)
    assert run(tmp_path, "propose", *place_args("https://a1.com/x", "https://www.post.gov.tw/x", **base)) == 7


def test_stats_counts_the_last_24_hours(tmp_path, pages, run, capsys):
    # 每日維護 09:45 跑 stats，查證排程 14:10 才跑：數字要看最近 24 小時，不是「今天」（審查員 1）
    assert two_rounds(tmp_path, pages, run) == 0
    capsys.readouterr()
    assert run(tmp_path, "stats", now=LATER + 20 * 3600) == 0
    line = capsys.readouterr().out
    assert "最近 24 小時：第一輪 0、收進 1、" in line and "今天寫入 0/10" in line
    assert run(tmp_path, "stats", now=LATER + 25 * 3600) == 0
    assert "收進 0、" in capsys.readouterr().out


def test_expired_first_round_counts_as_a_write(tmp_path, pages, run, capsys):
    pages.update({u: GOOD for u in ROUND1})
    assert run(tmp_path, "propose", *place_args(*ROUND1)) == 0
    pages.update({u: GOOD for u in ROUND2})
    later = NOW + 8 * DAY
    assert run(tmp_path, "confirm", *place_args(*ROUND2), now=later) == 10
    assert [e["action"] for e in events(tmp_path)][-1] == "expire"
    capsys.readouterr()
    assert run(tmp_path, "stats", now=later + 60) == 0
    line = capsys.readouterr().out
    assert "第一輪逾期 1" in line and "今天寫入 1/10" in line


def test_todo_lists_rechecks_on_mondays_by_itself(tmp_path, pages, run, capsys):
    assert two_rounds(tmp_path, pages, run) == 0
    monday = NOW + admin.RECHECK_DAYS * DAY
    while admin._day(monday).weekday() != 0:
        monday += DAY
    capsys.readouterr()
    assert run(tmp_path, "todo", now=monday + DAY) == 0
    assert "要重查" not in capsys.readouterr().out
    assert run(tmp_path, "todo", now=monday) == 0
    assert "要重查：甲麵館" in capsys.readouterr().out
    assert run(tmp_path, "todo", "--recheck", now=monday + DAY) == 0
    assert "要重查：甲麵館" in capsys.readouterr().out


def test_truncated_event_line_does_not_break_commands(tmp_path, pages, run):
    assert two_rounds(tmp_path, pages, run) == 0
    with (tmp_path / admin.EVENTS_NAME).open("ab") as f:
        f.write('{"action": "propose", "name": "甲'.encode("utf-8")[:-1])
    assert run(tmp_path, "stats", now=LATER + 60) == 0
    assert run(tmp_path, "todo", now=LATER + 60) == 0


def test_city_must_be_written_on_at_least_one_page(tmp_path, pages, run, capsys):
    # 中正區台北、基隆都有：只寫到區的頁面不能替縣市作證（審查員 1）
    district_only = f"<h2>{NAME}</h2><p>地址：中正區重慶南路一段122號 電話：{PHONE}</p>"
    pages.update({u: district_only for u in ROUND1})
    assert run(tmp_path, "propose", *place_args(*ROUND1)) == 7
    assert "縣市" in capsys.readouterr().err
    assert run(tmp_path, "propose", *place_args(*ROUND1, address="基隆市中正區重慶南路一段122號")) == 7
    pages.update({ROUND1[0]: GOOD})
    assert run(tmp_path, "propose", *place_args(*ROUND1)) == 0


def test_event_after_a_torn_line_is_still_readable(tmp_path, pages, run):
    # 上次寫到一半斷掉的那行沒有換行：這次的紀錄不能黏上去（否則讀不到、不算上限、不能撤回）
    log = tmp_path / admin.EVENTS_NAME
    log.write_bytes('{"action": "propose", "na'.encode("utf-8"))
    log.chmod(0o600)
    pages.update({u: GOOD for u in ROUND1})
    assert run(tmp_path, "propose", *place_args(*ROUND1)) == 0
    assert [e["action"] for e in admin._read_events(log)] == ["propose"]


def test_two_rounds_may_spell_the_branch_differently(tmp_path, pages, run):
    # 第一輪用括號、第二輪不用：name_key 相同，第二輪找得到第一輪
    name1, name2 = "鼎泰豐(信義店)", "鼎泰豐信義店"
    pages.update({u: page(name="鼎泰豐（信義店）") for u in ROUND1})
    assert run(tmp_path, "propose", *place_args(*ROUND1, name=name1)) == 0
    pages.update({u: page(name=name2) for u in ROUND2})
    assert run(tmp_path, "confirm", *place_args(*ROUND2, name=name2), now=LATER) == 0
    [place] = data(tmp_path)["places"]
    assert place["name_key"] == "鼎泰豐信義店" and place["status"] == "active"
