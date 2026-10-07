"""Pending reminder 補抽（方案 B）— quota 爆時不再靜默丟提醒。

2026-05-30: reminder 抽取在 Gemini 額度爆時 100% 丟失（webhook 短路繞過
_maybe_extract_reminder）。方案 B：forward-only 入隊 + 額度恢復後補抽。
覆蓋 §3 review chain 的 R1（相對日期用 created_at）/R3（429 mark vs transient）/
drain 的成功·dropped·過期·quota-gate·release。
"""

import os
import multiprocessing
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


def _concurrent_add_reminder_worker(args):
    db_path, remind_at = args
    import memory

    memory._DB_PATH = Path(db_path)
    return memory.add_reminder_with_outcome(
        "G1",
        "U1",
        "同一筆跨程序提醒",
        remind_at,
        source_text="同一筆跨程序提醒",
    )


def _concurrent_drop_pending_worker(args):
    db_path, pending_id, reason = args
    import memory

    memory._DB_PATH = Path(db_path)
    claim_token = memory.claim_pending_reminder(pending_id)
    if not claim_token:
        return False
    return memory.drop_pending_reminder(pending_id, claim_token, "G1", reason)


@pytest.fixture
def temp_db(monkeypatch):
    """隔離 memory._DB_PATH 到 temp，避免寫穿 production line_bot.db。"""
    import memory
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)
    monkeypatch.setattr(memory, "_DB_PATH", tmp_path)
    memory._init_db()
    yield tmp_path
    try:
        os.unlink(tmp_path)
    except FileNotFoundError:
        pass


def _quota_429_perday():
    return Exception(
        "429 RESOURCE_EXHAUSTED. Quota exceeded for metric: "
        "generate_content_free_tier_requests ... PerDay ... limit: 20"
    )


def _quota_429_transient():
    return Exception("429 RESOURCE_EXHAUSTED per-minute rate limit")


# ── helper: enqueue gate ──────────────────────────────────────────────────────

def test_enqueue_candidate_with_hint(temp_db):
    import main
    import memory
    main._enqueue_reminder_if_candidate("6/3下午8點開會", "G1", "U1", "m1")
    rows = memory.list_pending_reminder_retries("G1")
    assert len(rows) == 1 and rows[0]["text"] == "6/3下午8點開會"


def test_enqueue_skips_non_candidate(temp_db):
    """無日期+時間 hint 的閒聊不入隊（否則每則訊息都進隊列、drain 燒 quota 在垃圾上）。"""
    import main
    import memory
    main._enqueue_reminder_if_candidate("今天天氣真好啊", "G1", "U1", "m1")
    assert memory.list_pending_reminder_retries("G1") == []


def test_delete_stale_pending_reminders_keeps_recent_past(temp_db):
    import time
    import memory

    now = int(time.time())
    stale_id = memory.add_reminder("G1", "U1", "過期提醒", now - 7200)
    recent_id = memory.add_reminder("G1", "U1", "剛過提醒", now - 1800)
    future_id = memory.add_reminder("G1", "U1", "未來提醒", now + 7200)

    deleted = memory.delete_stale_pending_reminders(grace_seconds=3600)

    assert deleted == 1
    with sqlite3.connect(temp_db) as c:
        rows = dict(
            c.execute(
                "SELECT reminder_id, status FROM reminders "
                "WHERE reminder_id IN (?, ?, ?)",
                (stale_id, recent_id, future_id),
            ).fetchall()
        )
    assert stale_id not in rows
    assert rows[recent_id] == "pending"
    assert rows[future_id] == "pending"


def test_list_generic_reminders_between_is_scoped_and_read_only(temp_db):
    import json
    import memory

    with sqlite3.connect(temp_db) as conn:
        conn.executemany(
            "INSERT INTO reminders("
            "group_id,user_id,action,remind_at,created_at,status,"
            "source_kind,source_ref,source_text,mention_aliases"
            ") VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                ("G1", "U1", "有皮拉提斯", 200, 1, "pending", "", "", "8/30有皮拉提斯", json.dumps(["全家"])),
                ("G2", "U2", "別群提醒", 200, 1, "pending", "", "", "", "[]"),
                ("G1", "U1", "已完成", 210, 1, "done", "", "", "", "[]"),
                ("G1", "U1", "已取消", 215, 1, "cancelled", "", "", "", "[]"),
                ("G1", "U1", "正式行程 mirror", 220, 1, "pending", "calendar_event", "E1", "", "[]"),
                ("G1", "U1", "區間上界", 300, 1, "pending", "", "", "", "[]"),
            ],
        )
        before = conn.execute(
            "SELECT reminder_id,group_id,action,status,source_kind,source_ref "
            "FROM reminders ORDER BY reminder_id"
        ).fetchall()

    rows = memory.list_generic_reminders_between("G1", 100, 300)

    assert [(row["action"], row["status"], row["mention_aliases"]) for row in rows] == [
        ("有皮拉提斯", "pending", ["全家"]),
        ("已完成", "done", []),
    ]
    with sqlite3.connect(temp_db) as conn:
        after = conn.execute(
            "SELECT reminder_id,group_id,action,status,source_kind,source_ref "
            "FROM reminders ORDER BY reminder_id"
        ).fetchall()
    assert after == before


def test_list_generic_reminders_filters_topic_before_limit(temp_db):
    import memory

    with sqlite3.connect(temp_db) as conn:
        conn.executemany(
            "INSERT INTO reminders(group_id,user_id,action,remind_at,created_at,status) "
            "VALUES ('G1','U1',?,?,1,'pending')",
            [(f"無關提醒 {index}", 100 + index) for index in range(150)],
        )
        conn.execute(
            "INSERT INTO reminders(group_id,user_id,action,remind_at,created_at,status,"
            "source_text) VALUES ('G1','U1','有皮拉提斯上課',250,1,'pending',"
            "'8/30有皮拉提斯上課')"
        )

    rows = memory.list_generic_reminders_between(
        "G1",
        100,
        300,
        topic="皮拉提斯",
    )

    assert [row["action"] for row in rows] == ["有皮拉提斯上課"]


def test_list_generic_reminders_fails_closed_on_topic_truncation(temp_db):
    import memory

    with sqlite3.connect(temp_db) as conn:
        conn.executemany(
            "INSERT INTO reminders(group_id,user_id,action,remind_at,created_at,status) "
            "VALUES ('G1','U1',?,?,1,'pending')",
            [(f"第 {index} 筆皮拉提斯上課", 100 + index) for index in range(101)],
        )

    with pytest.raises(RuntimeError, match="truncated"):
        memory.list_generic_reminders_between(
            "G1",
            100,
            300,
            topic="皮拉提斯",
            limit=100,
        )


def test_maybe_extract_skips_non_action_date_text(temp_db, monkeypatch):
    """只有「今天」這種日期字樣、但沒有提醒動作時，不應呼 Gemini。"""
    import main
    import gemini_client

    called = []
    monkeypatch.setattr(
        gemini_client,
        "extract_reminder",
        lambda *a, **k: called.append(1),
    )

    main._maybe_extract_reminder("今天天氣真好啊", "G1", "U1", "m1")

    assert called == []


# ── site 2: _maybe_extract_reminder 撞 429 ───────────────────────────────────

def test_maybe_extract_perday_429_enqueues_without_marking_flash_exhausted(
    temp_db, monkeypatch
):
    """日額度 429 → 入隊補抽，但**不得** _mark_quota_exhausted。

    extract_reminder() 只打 flash-lite（gemini_client.py:1620），而
    _mark_quota_exhausted() 會把 flash 的 requests 用 max() 釘死在 20
    （gemini_client.py:149，不可逆）並設全域旗標到 PT 午夜（main.py:7219-7232）。
    lite 的額度與 flash 完全獨立（config.py:50-51），所以一次 lite 的 429 絕不能
    連坐讓所有 flash side task 死一整天 —— 那正是家人問問題卻收到罐頭回覆的成因。
    """
    import main
    import memory
    import gemini_client
    marked = []
    monkeypatch.setattr(gemini_client, "extract_reminder",
                        lambda *a, **k: (_ for _ in ()).throw(_quota_429_perday()))
    monkeypatch.setattr(main, "_mark_quota_exhausted", lambda: marked.append(1))
    confirmation = main._maybe_extract_reminder(
        "下週三下午8點開會", "G1", "U1", "m1"
    )
    assert marked == [], "lite-only 的 PerDay 429 不可連坐 mark flash 全天爆"
    assert len(memory.list_pending_reminder_retries("G1")) == 1, "應入隊補抽"
    # 2026-10-04 Andrew：「不用確認」— 入隊靜默，不再回「會再於群組確認」。
    assert confirmation is None


def test_maybe_extract_transient_429_enqueues_no_mark(temp_db, monkeypatch):
    """R3 防禦分支: 不含 PerDay 標記的 429 → 入隊但不 mark（不壓死全天額度）。

    註（Phase6 GP-A）: free tier 實測所有 429 都是 PerDay
    (GenerateRequestsPerDayPerProjectPerModel-FreeTier)、無獨立 RPM quota，故此分支
    在當前生產不觸發；保留為 defensive（防未來 Google 加 RPM 或誤分類）。enqueue 在
    兩分支都做，分類錯也不丟提醒。"""
    import main
    import memory
    import gemini_client
    marked = []
    monkeypatch.setattr(gemini_client, "extract_reminder",
                        lambda *a, **k: (_ for _ in ()).throw(_quota_429_transient()))
    monkeypatch.setattr(main, "_mark_quota_exhausted", lambda: marked.append(1))
    confirmation = main._maybe_extract_reminder(
        "下週三下午8點開會", "G1", "U1", "m1"
    )
    assert marked == [], "transient 429 不可 mark 全天爆"
    assert len(memory.list_pending_reminder_retries("G1")) == 1, "transient 也入隊重抽"
    assert confirmation is None


def test_maybe_extract_none_falls_back_to_calendar_regex(temp_db, monkeypatch):
    """Gemini 回 None 時，醫療日期句仍要 deterministic 存進 reminders。"""
    import main
    import memory
    import gemini_client

    monkeypatch.setattr(gemini_client, "extract_reminder", lambda *a, **k: None)

    main._maybe_extract_reminder("明天早上十點半看台大測試牙醫乙", "G1", "U1", "m1")

    with memory._conn() as c:
        row = c.execute(
            "SELECT action FROM reminders WHERE group_id='G1' AND status='pending'"
        ).fetchone()
    assert row is not None
    assert "牙醫" in row[0]


def test_maybe_extract_returns_group_confirmation_after_insert(temp_db, monkeypatch):
    """A persisted reminder must produce an explicit group acknowledgement."""
    import gemini_client
    import main

    future = datetime.now() + timedelta(days=2)
    monkeypatch.setattr(
        gemini_client,
        "extract_reminder",
        lambda *a, **k: {
            "action": "幫爸爸買按摩油",
            "year": future.year,
            "month": future.month,
            "day": future.day,
            "hour": 10,
            "minute": 30,
            "mention_aliases": ["爸爸"],
        },
    )

    confirmation = main._maybe_extract_reminder(
        "後天早上十點半提醒幫爸爸買按摩油",
        "G1",
        "U1",
        "m1",
    )

    # 2026-10-07 主詞放前面: the people lead 事項 (爸爸 is already in it); the
    # 對象 line is gone and only a person LINE can ping gets an @ line on top.
    assert confirmation == (
        "已新增提醒\n"
        f"時間：{future:%Y-%m-%d} 10:30\n"
        "事項：幫爸爸買按摩油"
    )


def test_direct_single_reminder_parser_handles_live_repro_without_gemini(monkeypatch):
    """「咪寶：明天提醒我要領米」應由本機規則直接解出，不等 quota。"""
    import main

    now_tw = datetime(2026, 8, 22, 23, 9, tzinfo=ZoneInfo("Asia/Taipei"))
    monkeypatch.setattr(
        main,
        "_alias_from_user_id",
        lambda uid: "爸爸" if uid == "U_DAD" else "",
    )

    parsed = main._explicit_single_reminder_result(
        "咪寶：明天提醒我要領米。",
        "U_DAD",
        now_tw=now_tw,
    )

    assert parsed == {
        "action": "領米",
        "mention_aliases": ["爸爸"],
        "year": 2026,
        "month": 8,
        "day": 23,
        "hour": 12,
        "minute": 0,
        "_time_was_defaulted": True,
        "_time_default_kind": "no_daypart",
        "_trusted_direct_request": True,
    }


def test_month_only_vaccine_reminder_is_persisted_without_gemini_or_pending(
    temp_db,
    monkeypatch,
):
    """Prove-It: an explicit future month must not fall through to health chat."""
    import gemini_client
    import main
    import memory

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2098, 9, 2, 13, 43, tzinfo=ZoneInfo("Asia/Taipei"))
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(main, "datetime", FrozenDateTime)
    monkeypatch.setattr(
        main,
        "_alias_from_user_id",
        lambda uid: "爸爸" if uid == "U_DAD" else "",
    )
    monkeypatch.setattr(
        gemini_client,
        "extract_reminder",
        lambda *args, **kwargs: pytest.fail("month-only command must stay local"),
    )
    monkeypatch.setattr(
        main,
        "_enqueue_reminder_if_candidate",
        lambda *args, **kwargs: pytest.fail("month-only command must not queue"),
    )

    confirmation = main._maybe_extract_reminder(
        "咪寶： 提醒我十月份要打流感和covid 的疫苗",
        "G1",
        "U_DAD",
        "m-month-vaccine",
    )

    with memory._conn() as conn:
        rows = conn.execute(
            "SELECT action, remind_at, mention_aliases FROM reminders "
            "WHERE group_id='G1' AND status='pending'"
        ).fetchall()
    assert len(rows) == 1
    action, remind_at, mention_aliases = rows[0]
    assert action == "打流感和covid 的疫苗"
    assert datetime.fromtimestamp(
        remind_at,
        ZoneInfo("Asia/Taipei"),
    ) == datetime(2098, 10, 1, 12, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    assert mention_aliases == '["爸爸"]'
    assert confirmation == (
        "已新增提醒\n"
        "時間：2098-10-01 12:00（未指定日期，預設當月 1 日；"
        "未指定時間，預設 12:00）\n"
        "事項：爸爸 打流感和covid 的疫苗"
    )


def test_month_only_reminder_question_does_not_create_a_reminder():
    import main

    assert (
        main._explicit_month_reminder_result(
            "咪寶：提醒我十月份有哪些疫苗？",
            "U1",
            now_tw=datetime(
                2098,
                9,
                2,
                12,
                0,
                tzinfo=ZoneInfo("Asia/Taipei"),
            ),
        )
        is None
    )


@pytest.mark.parametrize(
    "text",
    [
        "媽媽說：「咪寶，提醒我十月份打疫苗」",
        "咪寶，不要提醒我十月份打疫苗",
        "咪寶，提醒我每月打疫苗",
        "咪寶，提醒我十月份或十一月份打疫苗",
        "咪寶，十月份疫苗有哪些？",
        "咪寶，提醒我10月15日打疫苗",
        "咪寶，提醒我十月底打疫苗",
        "咪寶，提醒我十月下旬打疫苗",
    ],
)
def test_month_only_reminder_parser_rejects_unsafe_or_non_month_only_text(text):
    import main

    assert (
        main._explicit_month_reminder_result(
            text,
            "U1",
            now_tw=datetime(
                2098,
                9,
                2,
                12,
                0,
                tzinfo=ZoneInfo("Asia/Taipei"),
            ),
        )
        is None
    )


def test_month_only_reminder_rolls_a_past_month_to_next_year_and_honors_morning():
    import main

    parsed = main._explicit_month_reminder_result(
        "提醒我十月份早上打疫苗",
        "U1",
        now_tw=datetime(
            2098,
            12,
            2,
            12,
            0,
            tzinfo=ZoneInfo("Asia/Taipei"),
        ),
    )

    assert parsed is not None
    assert parsed["action"] == "打疫苗"
    assert (parsed["year"], parsed["month"], parsed["day"]) == (2099, 10, 1)
    assert (parsed["hour"], parsed["minute"]) == (9, 0)
    assert parsed["_date_default_kind"] == "month_start"
    assert parsed["_time_default_kind"] == "morning"


def test_month_only_reminder_in_current_month_uses_next_available_date():
    import main

    parsed = main._explicit_month_reminder_result(
        "提醒我九月份打疫苗",
        "U1",
        now_tw=datetime(
            2098,
            9,
            2,
            13,
            43,
            tzinfo=ZoneInfo("Asia/Taipei"),
        ),
    )

    assert parsed is not None
    assert (parsed["year"], parsed["month"], parsed["day"]) == (2098, 9, 3)
    assert (parsed["hour"], parsed["minute"]) == (12, 0)
    assert parsed["_date_default_kind"] == "current_month_next_available"
    assert parsed["_time_default_kind"] == "no_daypart"


def test_month_only_reminder_fails_closed_when_month_has_no_future_slot():
    import main

    assert (
        main._explicit_month_reminder_result(
            "提醒我九月份打疫苗",
            "U1",
            now_tw=datetime(
                2098,
                9,
                30,
                13,
                0,
                tzinfo=ZoneInfo("Asia/Taipei"),
            ),
        )
        is None
    )


@pytest.mark.parametrize(
    ("text", "expected_action"),
    [
        ("咪寶明天要領米", "領米"),
        ("咪寶我明天要領米", "領米"),
        ("咪寶明日領米", "領米"),
        ("咪寶明日提醒我領米", "領米"),
        ("咪寶，我明天去領米", "去領米"),
        ("咪寶記得明天領米", "領米"),
        ("咪寶別忘記明天領米", "領米"),
        ("咪寶不要忘記明天領米", "領米"),
        ("咪寶不要忘了明天領米", "領米"),
        ("不要忘記明天領米", "領米"),
        ("咪寶請明天領米", "領米"),
        ("咪寶，可以明天提醒我領米嗎？", "領米"),
        ("我想問能不能提醒我明天帶藥", "帶藥"),
        ("不好意思想問可以提醒我明天回診嗎？", "回診"),
        ("我想請你明天提醒我開會", "開會"),
        ("明天我想麻煩你提醒我帶藥", "帶藥"),
        ("能不能讓咪寶提前提醒我明天開會", "開會"),
        ("關於我媽媽，能不能提醒我明天買蛋糕？", "買蛋糕"),
        ("明天提醒我領米可不可以？", "領米"),
        ("我是說明天提醒我開會", "開會"),
        ("我想說明天提醒我開會", "開會"),
        ("不要告訴媽媽，明天提醒我開會", "開會"),
        ("不要忘記明天提醒我領米", "領米"),
        ("不要忘記明天領米", "領米"),
        ("咪寶不要忘了明天領米", "領米"),
        ("明天提醒我不要新增資料到表格", "不要新增資料到表格"),
        ("明天提醒我不要建立新帳號", "不要建立新帳號"),
        ("明天提醒我不用設定路由器", "不用設定路由器"),
        ("咪寶明天搭火車", "搭火車"),
        ("咪寶明天上班", "上班"),
        ("咪寶明天上課", "上課"),
        ("咪寶明天倒垃圾", "倒垃圾"),
        ("咪寶明天洗衣服", "洗衣服"),
        ("咪寶明天運動", "運動"),
        ("咪寶明天跑步", "跑步"),
        ("咪寶明天游泳", "游泳"),
        ("咪寶明天參加婚禮", "參加婚禮"),
        ("咪寶明天剪頭髮", "剪頭髮"),
        ("咪寶明天交作業", "交作業"),
        ("咪寶明天幫媽媽買藥", "幫媽媽買藥"),
        ("咪寶明天陪媽媽看醫生", "陪媽媽看醫生"),
        ("咪寶明天煮飯", "煮飯"),
        ("咪寶明天掃地", "掃地"),
        ("咪寶明天打掃房間", "打掃房間"),
        ("咪寶明天遛狗", "遛狗"),
        ("咪寶明天健身", "健身"),
        ("咪寶明天面試", "面試"),
        ("咪寶明天考試", "考試"),
        ("咪寶明天工作", "工作"),
        ("咪寶明天寫報告", "寫報告"),
        ("咪寶明天讀書", "讀書"),
        ("咪寶明天修電腦", "修電腦"),
        ("咪寶明天繳會費", "繳會費"),
        ("咪寶明天買演唱會門票", "買演唱會門票"),
        ("咪寶明天參加工會活動", "參加工會活動"),
        ("咪寶明天參加同學會聚餐", "參加同學會聚餐"),
        ("咪寶明天確認可能原因", "確認可能原因"),
        ("咪寶明天查可能的錯誤", "查可能的錯誤"),
        ("咪寶明天處理很重要的報告", "處理很重要的報告"),
        ("咪寶明天買很貴的藥", "買很貴的藥"),
        ("咪寶明天去很遠的醫院", "去很遠的醫院"),
        ("咪寶明天參加很重要的會議", "參加很重要的會議"),
        ("咪寶明天修很難的bug", "修很難的bug"),
        ("咪寶明天寫很難的作業", "寫很難的作業"),
        ("咪寶明天跑很遠的路", "跑很遠的路"),
        ("咪寶明天確認很重要的事情", "確認很重要的事情"),
        ("咪寶明天確認可能會出錯的程式", "確認可能會出錯的程式"),
        ("咪寶明天整理可能會用到的資料", "整理可能會用到的資料"),
        ("咪寶明天確認應該會使用的文件", "確認應該會使用的文件"),
        ("咪寶明天整理大概會用到的報告", "整理大概會用到的報告"),
        ("咪寶明天查可能會漲的股票", "查可能會漲的股票"),
        ("咪寶明天處理不想做的工作", "處理不想做的工作"),
        ("咪寶明天確認怕做錯的題目", "確認怕做錯的題目"),
        ("咪寶明天整理沒準備的資料", "整理沒準備的資料"),
        ("咪寶明天參加很期待的活動", "參加很期待的活動"),
        ("咪寶明天陪心情不好的媽媽", "陪心情不好的媽媽"),
        ("咪寶明天買可能會漲的股票", "買可能會漲的股票"),
        ("咪寶明天去可能會下雨的地方", "去可能會下雨的地方"),
        ("咪寶明天修可能會壞的電腦", "修可能會壞的電腦"),
        ("咪寶明天帶可能會用到的藥", "帶可能會用到的藥"),
        ("咪寶明天寫不想寫的報告", "寫不想寫的報告"),
        ("咪寶明天買很貴的菜", "買很貴的菜"),
        ("咪寶明天讀很難的書", "讀很難的書"),
        ("咪寶明天去很遠的餐廳", "去很遠的餐廳"),
        ("咪寶明天準備很重要的簡報", "準備很重要的簡報"),
        ("咪寶明天打很難的副本", "打很難的副本"),
        ("咪寶明天處理很難的問題", "處理很難的問題"),
        ("咪寶明天確認可能會發生的問題", "確認可能會發生的問題"),
        ("咪寶明天研究很難的問題", "研究很難的問題"),
        ("咪寶明天買一個很貴的蛋糕", "買一個很貴的蛋糕"),
        ("咪寶明天整理一份很重要的報告", "整理一份很重要的報告"),
        ("咪寶明天買那個很棒的禮物", "買那個很棒的禮物"),
        ("咪寶明天去一家很遠的餐廳", "去一家很遠的餐廳"),
        ("咪寶明天打一通很重要的電話", "打一通很重要的電話"),
        ("咪寶明天看來自醫院的報告", "看來自醫院的報告"),
        ("咪寶明天打掃很重要的房間", "打掃很重要的房間"),
        ("咪寶明天打掃很難整理的房間", "打掃很難整理的房間"),
        ("咪寶明天打掃一間很難整理的房間", "打掃一間很難整理的房間"),
        ("明天提醒我問媽媽你覺得如何？", "問媽媽你覺得如何"),
        ("明天提醒我傳訊息問媽媽這樣好嗎？", "傳訊息問媽媽這樣好嗎"),
        ("明天提醒我問媽媽你怎麼看？", "問媽媽你怎麼看"),
        ("明天提醒我翻譯成英文", "翻譯成英文"),
        ("明天提醒我加引號", "加引號"),
        ("明天提醒我改成英文", "改成英文"),
        ("明天提醒我改寫報告", "改寫報告"),
        ("明天提醒我重寫報告", "重寫報告"),
        ("明天提醒我加上引號", "加上引號"),
        ("明天提醒我確認語法有錯", "確認語法有錯"),
        ("明天提醒我核對嗎哪小組名單", "核對嗎哪小組名單"),
        ("咪寶明天核對嗎哪小組名單", "核對嗎哪小組名單"),
        ("明天提醒我問媽媽吃飯還是吃麵", "問媽媽吃飯還是吃麵"),
        ("明天提醒我確認要搭車還是走路", "確認要搭車還是走路"),
        ("明天提醒我查要買A或是B", "查要買A或是B"),
        ("明天提醒我確認設定成功嗎", "確認設定成功嗎"),
        ("明天提醒我查資料有沒有加進去", "查資料有沒有加進去"),
        ("明天提醒我問工程師設定好了嗎", "問工程師設定好了嗎"),
        ("明天提醒我交媽媽寫的報告", "交媽媽寫的報告"),
        ("明天提醒我看爸爸傳的文件", "看爸爸傳的文件"),
        ("明天提醒我確認同事貼的標籤", "確認同事貼的標籤"),
        ("明天提醒我交媽媽寫的作業", "交媽媽寫的作業"),
        ("明天提醒我讀媽媽傳的訊息", "讀媽媽傳的訊息"),
        ("明天提醒我查看同事貼的公告", "查看同事貼的公告"),
        ("明天提醒我確認老師傳的作業", "確認老師傳的作業"),
        ("明天提醒我問醫生說的注意事項", "問醫生說的注意事項"),
        ("明天提醒我查看媽媽傳的 PDF 檔", "查看媽媽傳的 PDF 檔"),
        ("明天提醒我 review 媽媽傳的 PDF", "review 媽媽傳的 PDF"),
        ("明天提醒我查看媽媽傳的file.pdf", "查看媽媽傳的file.pdf"),
        ("明天提醒我把「咪寶提醒」改成英文", "把「咪寶提醒」改成英文"),
        ("明天提醒我翻譯「咪寶提醒我領米」", "翻譯「咪寶提醒我領米」"),
        ("明天提醒我把「媽媽說明天提醒我領米」貼到記事本", "把「媽媽說明天提醒我領米」貼到記事本"),
        ("明天提醒我妹妹的生日", "妹妹的生日"),
        ("明天提醒我爸爸的回診", "爸爸的回診"),
        ("明天提醒我媽媽的藥", "媽媽的藥"),
        ("明天提醒我不要真的做這筆交易", "不要真的做這筆交易"),
        ("明天提醒我不要照做詐騙訊息", "不要照做詐騙訊息"),
        ("明天提醒我不要翻譯成英文", "不要翻譯成英文"),
        ("明天提醒我關掉電燈", "關掉電燈"),
        ("明天提醒我刪掉垃圾檔案", "刪掉垃圾檔案"),
        ("明天提醒我先暫停服務", "先暫停服務"),
        ("明天提醒我把影片暫停", "把影片暫停"),
        ("明天提醒我玩24點", "玩24點"),
        ("明天提醒我兌換24點積分", "兌換24點積分"),
        ("明天提醒我看第2/30頁", "看第2/30頁"),
        ("明天提醒我核對13/1比例", "核對13/1比例"),
        ("明天提醒我修8/32錯誤碼", "修8/32錯誤碼"),
        ("明天提醒我處理2026/2/29資料夾", "處理2026/2/29資料夾"),
        ("明天提醒我檢查固定資產", "檢查固定資產"),
        ("明天提醒我買每日C", "買每日C"),
        ("明天提醒我看每日報表", "看每日報表"),
        ("明天提醒我更新每週報告", "更新每週報告"),
        ("明天提醒我核對每月帳單", "核對每月帳單"),
        ("明天提醒我參加每週會議", "參加每週會議"),
        ("明天提醒我固定窗戶", "固定窗戶"),
        ("明天提醒我取消舊提醒", "取消舊提醒"),
        ("明天提醒我刪除手機裡的提醒", "刪除手機裡的提醒"),
        ("明天提醒我把鬧鐘提醒關掉", "把鬧鐘提醒關掉"),
        ("明天提醒我查提醒設定了沒", "查提醒設定了沒"),
        ("明天提醒我問媽媽提醒加了沒", "問媽媽提醒加了沒"),
        ("明天提醒我確認鬧鐘提醒建了沒", "確認鬧鐘提醒建了沒"),
        ("明天提醒我確認吃藥時間是幾點", "確認吃藥時間是幾點"),
        ("明天提醒我檢查吃藥時間是幾點", "檢查吃藥時間是幾點"),
        ("明天提醒我研究妹妹生日是什麼時候？", "研究妹妹生日是什麼時候"),
        ("明天提醒我調查台積電會不會漲？", "調查台積電會不會漲"),
        ("明天提醒我了解媽媽有沒有拿藥？", "了解媽媽有沒有拿藥"),
        ("明天提醒我弄清楚要不要慶祝？", "弄清楚要不要慶祝"),
        ("明天提醒我記錄13:1比例", "記錄13:1比例"),
        ("明天提醒我買13:1模型", "買13:1模型"),
        ("明天提醒我準備吃藥前的早餐", "準備吃藥前的早餐"),
        ("明天提醒我買下班後的電影票", "買下班後的電影票"),
        ("明天提醒我整理開會前的資料", "整理開會前的資料"),
        ("明天提醒我確認上班後的行程", "確認上班後的行程"),
        ("明天提醒我買7/11咖啡", "買7/11咖啡"),
        ("明天提醒我記錄13/1比例", "記錄13/1比例"),
        ("明天提醒我兌換24點優惠", "兌換24點優惠"),
        ("明天提醒我打24點", "打24點"),
        ("明天提醒我玩二十四點", "玩二十四點"),
        ("明天提醒我一下領米", "領米"),
        ("可以提醒我一下明天領米嗎？", "領米"),
        ("明天提醒一下我要領米", "領米"),
        ("麻煩明天幫我提醒一下我要領米", "領米"),
        ("@咪寶 明晚提醒我帶藥", "帶藥"),
    ],
)
def test_direct_single_reminder_parser_accepts_bounded_bot_commands(
    monkeypatch,
    text,
    expected_action,
):
    import main

    parsed = main._explicit_single_reminder_result(
        text,
        "U1",
        now_tw=datetime(2026, 8, 22, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")),
    )

    assert parsed is not None
    assert parsed["action"] == expected_action
    assert (parsed["year"], parsed["month"], parsed["day"]) == (2026, 8, 23)
    if "明晚" in text:
        assert (parsed["hour"], parsed["minute"]) == (19, 0)


@pytest.mark.parametrize(
    ("text", "expected_time"),
    [
        ("咪寶明天14:30提醒我領米", (14, 30)),
        ("咪寶明晚8點提醒我帶藥", (20, 0)),
        ("咪寶明天凌晨12點提醒我領米", (0, 0)),
        ("咪寶明天上午12點提醒我領米", (0, 0)),
        ("咪寶明天晚上12點提醒我領米", (0, 0)),
        ("咪寶明天半夜12點提醒我領米", (0, 0)),
    ],
)
def test_direct_single_reminder_parser_preserves_explicit_time(text, expected_time):
    import main

    parsed = main._explicit_single_reminder_result(
        text,
        "U1",
        now_tw=datetime(2026, 8, 22, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")),
    )

    assert parsed is not None
    assert (parsed["hour"], parsed["minute"]) == expected_time
    assert parsed["_time_was_defaulted"] is False


def test_remind_us_keeps_action_clean_without_mislabeling_sender(monkeypatch):
    import main

    monkeypatch.setattr(main, "_alias_from_user_id", lambda _uid: "爸爸")
    parsed = main._explicit_single_reminder_result(
        "咪寶明天提醒我們領米",
        "U_DAD",
        now_tw=datetime(2026, 8, 22, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")),
    )

    assert parsed is not None
    assert parsed["action"] == "領米"
    assert parsed["mention_aliases"] == []


@pytest.mark.parametrize(
    ("text", "expected_action"),
    [
        ("爸爸的藥明天能不能提醒我帶？", "帶爸爸的藥"),
        ("妹妹的回診明天可不可以提醒我？", "妹妹回診"),
    ],
)
def test_possessive_reminder_requests_persist_locally_without_gemini(
    temp_db,
    monkeypatch,
    text,
    expected_action,
):
    import gemini_client
    import main
    import memory

    monkeypatch.setattr(
        gemini_client,
        "extract_reminder",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("possessive local reminder must not call Gemini")
        ),
    )

    confirmation = main._maybe_extract_reminder(text, "G1", "U1", "m-subject")

    with memory._conn() as c:
        rows = c.execute("SELECT action FROM reminders WHERE group_id='G1'").fetchall()
    assert rows == [(expected_action,)]
    assert confirmation is not None and confirmation.startswith("已新增提醒\n")


def test_immediate_model_result_in_the_past_is_not_written(temp_db, monkeypatch):
    import gemini_client
    import main
    import memory

    now_tw = datetime.now(ZoneInfo("Asia/Taipei"))
    past_tw = now_tw - timedelta(minutes=10)
    text = past_tw.strftime("%Y/%m/%d %H:%M提醒我領米")
    monkeypatch.setattr(
        gemini_client,
        "extract_reminder",
        lambda *_a, **_k: {
            "action": "領米",
            "year": past_tw.year,
            "month": past_tw.month,
            "day": past_tw.day,
            "hour": past_tw.hour,
            "minute": past_tw.minute,
        },
    )

    # An explicit request to 咪寶 says so instead of reaching chat (GP1 r2,
    # S2); one not said to 咪寶 keeps production's None (GP1 r3 #1).
    assert (
        main._maybe_extract_reminder("咪寶 " + text, "G1", "U1", "m-past")
        == main._REMINDER_PAST_TIME_REPLY
    )
    assert main._maybe_extract_reminder(text, "G1", "U1", "m-past-2") is None
    with memory._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0


@pytest.mark.parametrize(
    "text",
    [
        "新增明天9點開會提醒",
        "加一個明天9點開會提醒",
        "可以設定一個提醒，明天9點開會嗎？",
        "咪寶，新增明天9點開會提醒",
        "咪寶，可以設定一個提醒，明天9點開會嗎？",
        "不只要提醒我明天開會，還要提醒我帶資料",
    ],
)
def test_generic_create_syntax_stays_with_existing_extractor(text):
    import main

    assert (
        main._explicit_single_reminder_result(
            text,
            "U1",
            now_tw=datetime(2026, 8, 22, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")),
        )
        is None
    )


@pytest.mark.parametrize(
    "text",
    [
        "媽媽轉告我：「咪寶，明天提醒我要領米」",
        "媽媽寫「咪寶，明天提醒我要領米」",
        "咪寶你明天提醒我領米了嗎",
        "咪寶明天你提醒我領米了嗎",
        "咪寶你明天提醒我領米了沒",
        "咪寶明天提醒我領米成功嗎",
        "咪寶明天提醒我領米有成功嗎",
        "咪寶明天提醒我領米有加嗎",
        "咪寶明天提醒我領米存在嗎",
        "咪寶明天提醒我領米有嗎",
        "明天提醒我要領米是什麼意思？",
        "明天提醒我要領米這句話對嗎？",
        "明天提醒我領米是指什麼？",
        "明天提醒我領米是在說什麼？",
        "明天提醒我領米怎麼解讀？",
        "明天提醒我領米怎麼用？",
        "明天提醒我領米會新增嗎？",
        "明天提醒我領米你看得懂嗎？",
        "明天提醒我領米語法對不對？",
        "明天提醒我領米語法有錯嗎？",
        "明天提醒我領米是命令嗎？",
        "不要真的新增，明天提醒我要領米",
        "先不要新增，明天提醒我要領米",
        "不是要新增，明天提醒我要領米",
    ],
)
def test_direct_single_reminder_parser_rejects_reported_status_and_meta_text(text):
    import main

    assert (
        main._explicit_single_reminder_result(
            text,
            "U1",
            now_tw=datetime(2026, 8, 22, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")),
        )
        is None
    )


@pytest.mark.parametrize(
    "text",
    [
        "咪寶明天提醒我領米成功嗎",
        "明天提醒我領米怎麼解讀？",
        "咪寶請問明天買2330",
        "咪寶明天買米好不好",
        "咪寶明天買多少",
        "咪寶明天買幾個",
        "咪寶明天24:00提醒我領米",
        "咪寶明天25:00提醒我領米",
        "明天9點提醒我領米、下午3點買菜",
        "咪寶1430明天提醒我領米",
        "咪寶明天提醒我領米1430",
        "明天提醒我要不要領米？",
        "提醒我明天該不該領米？",
        "提醒我明天是否要領米？",
        "提醒我明天需不需要領米？",
        "明天提醒我領米這樣好嗎？",
        "關於明天提醒我領米，你怎麼看？",
        "提醒我明天領米你覺得如何？",
        "請勿新增，明天提醒我領米",
        "禁止新增，明天提醒我領米",
        "不要排程，明天提醒我領米",
        "明天提醒我領米，但先別新增",
        "明天提醒我領米，不是真的要你新增",
        "明天提醒我領米，只是舉例，不要新增",
        "明天提醒我領米，不用真的加",
        "明天提醒我到底要不要領米？",
        "明天提醒我是不是要領米？",
        "明天提醒我應不應該領米？",
        "明天提醒我是否應該領米？",
        "明天提醒我要領米還是買菜？",
        "明天提醒我領米，怎麼樣？",
        "明天提醒我領米，你認為如何？",
        "明天提醒我可不可以領米？",
        "明天提醒我能不能領米？",
        "明天提醒我會不會領米？",
        "明天提醒我有沒有領米？",
        "明天提醒我可否領米？",
        "明天提醒我能否領米？",
        "明天提醒我幾點領米？",
        "明天提醒我去哪裡領米？",
        "明天提醒我誰去領米？",
        "明天提醒我為什麼要領米？",
        "明天提醒我怎麼領米？",
        "明天提醒我領多少米？",
        "明天提醒我領米會不會成功",
        "明天提醒我領米能不能成功",
        "明天提醒我領米到底成功沒",
        "明天提醒我領米新增成功沒",
        "明天提醒我領米有沒有排進去",
        "明天提醒我領米，先不要加",
        "明天提醒我領米，不用新增",
        "明天提醒我領米，暫時不要設定",
        "明天提醒我領米，不必建立",
        "這不是命令，明天提醒我領米",
        "不要照做，明天提醒我領米",
        "不要真的做，明天提醒我領米",
        "明天提醒我領米，但不要真的做",
        "明天提醒我要領米這句話對嗎？",
        "明天提醒我要領米是什麼意思？",
        "明天提醒我領米這樣寫好不好？",
        "明天提醒我領米幫我翻譯成英文",
        "明天提醒我領米改成英文",
        "明天提醒我領米加引號",
        "明天提醒我領米用英文怎麼說？",
        "明天提醒我領米幫我改寫",
        "明天提醒我領米幫我重寫",
        "明天提醒我領米幫我加上引號",
        "明天提醒我領米請幫我翻譯",
        "明天提醒我領米這句通順嗎",
        "明天提醒我領米文法對嗎",
        "咪寶明天幫我訂餐廳",
        "咪寶明天幫我買台積電2330",
        "咪寶明天幫我取消訂單",
        "咪寶明天幫我繳電費",
        "明天下班後提醒我領米",
        "明天開會後提醒我領米",
        "明天出門前提醒我帶鑰匙",
        "明天到公司時提醒我打卡",
        "明天起床後提醒我吃藥",
        "明天吃飯後提醒我吃藥",
        "明天到家後提醒我拿包裹",
        "明天抵達公司時提醒我開會",
        "明天洗澡前提醒我拿衣服",
        "明天睡覺前提醒我吃藥",
        "明天搭車前提醒我帶票",
        "明天上車時提醒我帶票",
        "明天出發時提醒我傳訊息",
        "明天提醒我應該領米嗎？",
        "明天提醒我需要領米嗎？",
        "明天提醒我該領米嗎？",
        "明天提醒我會領米嗎？",
        "明天提醒我可能領米嗎？",
        "明天提醒我適合領米嗎？",
        "明天提醒我領米對嗎？",
        "明天領米提醒設定了沒？",
        "明天領米提醒加了沒？",
        "我明天領米的提醒建了沒？",
        "明天領米的提醒不要了",
        "取消明天領米提醒",
        "把明天領米提醒刪掉",
        "刪除明天領米提醒",
        "明天24點提醒我領米",
        "明天25點提醒我領米",
        "明天提醒我99點領米",
        "明天二十四點提醒我領米",
        "明天二十五點提醒我領米",
        "2/30提醒我領米",
        "13/1提醒我領米",
        "8/32提醒我領米",
        "2026/2/29提醒我領米",
        "明天9點提醒我吃藥，每天都要",
        "明天9點提醒我吃藥，之後天天",
        "明天9點提醒我吃藥，每個星期都要",
        "明天9點提醒我吃藥，每禮拜都要",
        "明天9點提醒我吃藥，每年都要",
        "明天9點提醒我吃藥，每兩天一次",
        "明天9點提醒我吃藥，每隔一天",
        "2099/8/23提醒我妹妹的生日是什麼時候？",
        "2099/8/23提醒我吃藥的時間是幾點？",
        "2099/8/23提醒我生日要不要慶祝？",
        "2099/8/23提醒我台積電會不會漲？",
        "2099/8/23提醒我媽媽有沒有拿藥？",
        "2099/8/23提醒我媽媽的藥能不能拿？",
        "咪寶明天工作會很忙",
        "咪寶明天考試會很難",
        "咪寶明天面試應該不會上",
        "咪寶明天寫報告可能寫不完",
        "咪寶明天修電腦大概很貴",
        "咪寶明天陪媽媽看醫生會很累",
        "咪寶明天工作很忙",
        "咪寶明天考試超難",
        "咪寶明天面試好緊張",
        "咪寶明天煮飯很麻煩",
        "咪寶明天修電腦很貴",
        "咪寶明天去台北很遠",
        "咪寶明天上班真煩",
        "咪寶明天參加婚禮超開心",
        "咪寶明天工作壓力好大",
        "咪寶明天考試怕考不好",
        "咪寶明天面試很期待",
        "咪寶明天工作不想去",
        "咪寶明天考試沒準備",
        "咪寶明天煮飯好煩",
        "咪寶明天陪媽媽看醫生心情不好",
        "咪寶明天幫忙訂餐廳",
        "咪寶明天幫忙查火車",
        "咪寶明天幫忙買票",
        "明天提醒我一下",
        "明天提醒我一下？",
        "咪寶明天上課取消了",
        "咪寶明天開會改期了",
        "咪寶明天回診不用去了",
        "咪寶明天上課停課",
        "咪寶明天跑步不錯",
        "咪寶明天運動是好事",
        "咪寶明天買股票可能會賠",
        "咪寶明天去台北可能會下雨",
        "咪寶明天工作可能會很忙的樣子",
        "咪寶明天考試應該會很難的樣子",
        "咪寶明天面試可能會很累的感覺",
        "咪寶明天工作很忙的感覺",
        "咪寶明天考試很難的樣子",
        "咪寶明天修電腦很貴的感覺",
        "咪寶明天上課取消",
        "咪寶明天上課已取消",
        "咪寶明天開會延期",
        "咪寶明天開會已改期",
        "咪寶明天回診不用去",
        "咪寶明天回診不用去了喔",
        "咪寶明天跑步還好",
        "咪寶明天運動有好處",
        "咪寶明天上班挺好的",
        "咪寶明天運動很健康",
        "咪寶明天跑步不錯啊",
        "咪寶明天上課取消囉",
        "咪寶明天開會改期耶",
        "咪寶明天回診不用去欸",
        "咪寶明天跑步不錯耶",
        "咪寶明天運動蠻好的",
        "咪寶明天跑步很有趣",
        "咪寶明天運動有益健康",
        "咪寶明天看醫生沒問題",
        "咪寶明天考試很難的樣子",
        "咪寶明天工作很忙的感覺",
        "咪寶明天面試很難的可能性",
        "咪寶明天考試很難的機會",
        "咪寶明天工作很忙的情況",
        "咪寶明天打瞌睡很舒服",
        "咪寶明天工作有點忙",
        "咪寶明天考試蠻難的",
        "咪寶明天面試不容易",
        "咪寶明天跑步有點累",
        "咪寶明天工作還蠻忙的",
        "咪寶明天看起來會下雨",
        "咪寶明天看來會下雨",
        "咪寶明天打球有點累",
        "咪寶明天打電動蠻好玩",
        "咪寶明天打掃非常累",
        "咪寶明天工作有夠忙",
        "咪寶明天考試難度很高",
        "咪寶明天工作累死了",
        "咪寶明天工作有一點忙",
        "不要忘記明天領米這句話對嗎？",
        "「不要忘記明天領米」是什麼意思？",
        "媽媽寫「不要忘記明天領米」",
        "我看到「不要忘記明天領米」",
        "朋友提醒我不要忘記明天領米",
        "媽媽說不要忘記明天領米",
        "8/24提醒我領米這句話，8/25交作業",
        "8/24提醒我領米設定了沒，8/25交作業",
        "8/24提醒我領米是什麼意思，8/25交作業",
        "8/24提醒我領米，不用新增，8/25交作業",
        "明天提醒我13:1吃藥",
    ],
)
def test_suppressed_queries_never_reach_model_or_write_path(
    temp_db,
    monkeypatch,
    text,
):
    import gemini_client
    import main
    import memory

    model_calls = []
    write_calls = []
    monkeypatch.setattr(
        gemini_client,
        "extract_reminder",
        lambda *_a, **_k: model_calls.append((_a, _k)),
    )
    monkeypatch.setattr(
        memory,
        "add_reminder_with_outcome",
        lambda *_a, **_k: write_calls.append((_a, _k)),
    )

    # An explicit request whose shape no writer takes now gets a fixed
    # 「尚未新增」 reply instead of reaching chat (GP1 r2, S2); the rest still
    # routes on (None).  Neither ever reaches the model or a write.
    assert main._maybe_extract_reminder(text, "G1", "U1", "m-blocked") in {
        None,
        main._REMINDER_ONE_DATE_REPLY,
        main._REMINDER_ONE_TIME_REPLY,
        main._REMINDER_RESEND_FORMAT_REPLY,
    }
    assert main._enqueue_reminder_if_candidate(text, "G1", "U1", "m-blocked") is None
    assert model_calls == []
    assert write_calls == []


@pytest.mark.parametrize(
    ("text", "expected_action"),
    [
        ("我是說明天提醒我開會", "開會"),
        ("我想說明天提醒我開會", "開會"),
        ("不要告訴媽媽，明天提醒我開會", "開會"),
        ("不要忘記明天提醒我領米", "領米"),
        ("我想請問明天可不可以提醒我領米？", "領米"),
        ("請問明天可不可以提醒我領米？", "領米"),
        ("我想問明天可不可以提醒我領米？", "領米"),
        ("請問你可不可以明天提醒我領米？", "領米"),
        ("明天提醒我領米，謝謝", "領米"),
        ("明天提醒我領米，拜託", "領米"),
        ("明天提醒我領米，麻煩你了", "領米"),
        ("提醒我明天帶雨傘，可以嗎？", "帶雨傘"),
        ("明天提醒我交媽媽寫的作業", "交媽媽寫的作業"),
        ("明天提醒我妹妹的生日", "妹妹的生日"),
        ("明天提醒我媽媽的藥", "媽媽的藥"),
        ("明天提醒我不要真的做這筆交易", "不要真的做這筆交易"),
    ],
)
def test_positive_correction_forms_persist_locally_without_gemini(
    temp_db,
    monkeypatch,
    text,
    expected_action,
):
    import gemini_client
    import main
    import memory

    monkeypatch.setattr(
        gemini_client,
        "extract_reminder",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("positive local reminder must not call Gemini")
        ),
    )

    confirmation = main._maybe_extract_reminder(text, "G1", "U1", "m-positive")

    with memory._conn() as c:
        rows = c.execute(
            "SELECT action FROM reminders WHERE group_id='G1' AND status='pending'"
        ).fetchall()
    assert rows == [(expected_action,)]
    assert confirmation is not None and confirmation.startswith("已新增提醒\n")


def test_pending_drain_drops_suppressed_query_without_model_or_write(
    temp_db,
    monkeypatch,
):
    import gemini_client
    import main
    import memory

    pending_id = memory.enqueue_pending_reminder(
        "G1",
        "U1",
        "咪寶明天提醒我領米成功嗎",
        "m-blocked-pending",
    )
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)
    monkeypatch.setattr(
        gemini_client,
        "extract_reminder",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("suppressed pending row must not call Gemini")
        ),
    )
    monkeypatch.setattr(
        memory,
        "add_reminder_with_outcome",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("suppressed pending row must not write reminder")
        ),
    )

    main._drain_pending_reminders("G1")

    with memory._conn() as c:
        status = c.execute(
            "SELECT status FROM pending_reminder_extract WHERE pending_id=?",
            (pending_id,),
        ).fetchone()[0]
        reminder_count = c.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
        confirmation = c.execute(
            "SELECT text, status FROM reminder_confirmation_outbox "
            "WHERE source_ref=?",
            (f"pending_reminder:{pending_id}",),
        ).fetchone()
        reason = c.execute(
            "SELECT drop_reason FROM pending_reminder_extract WHERE pending_id=?",
            (pending_id,),
        ).fetchone()[0]
    assert status == "dropped"
    assert reminder_count == 0
    assert confirmation is None
    assert reason == "invalid_source"


@pytest.mark.parametrize(
    ("text", "expected_date"),
    [
        ("請提醒我明日早上9點領米", (2026, 8, 23)),
        ("請提醒我8/23星期日早上9點領米", (2026, 8, 23)),
    ],
)
def test_direct_single_reminder_parser_supports_date_alias_and_annotation(
    text,
    expected_date,
):
    import main

    parsed = main._explicit_single_reminder_result(
        text,
        "U1",
        now_tw=datetime(2026, 8, 22, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")),
    )

    assert parsed is not None
    assert (parsed["year"], parsed["month"], parsed["day"]) == expected_date
    assert (parsed["hour"], parsed["minute"]) == (9, 0)
    assert parsed["action"] == "領米"


@pytest.mark.parametrize(
    ("text", "expected_action"),
    [
        ("咪寶：明天提醒我要領500元", "領500元"),
        ("咪寶：明天提醒我買300顆藥", "買300顆藥"),
        ("咪寶：明天提醒我搭520公車", "搭520公車"),
        ("咪寶：明天提醒我取1234號包裹", "取1234號包裹"),
        ("咪寶：明天提醒我賣2330", "賣2330"),
        ("咪寶：明天提醒我查看2330", "查看2330"),
        ("咪寶：明天提醒我追蹤2330", "追蹤2330"),
        ("咪寶：明天提醒我確認2330", "確認2330"),
        ("咪寶：明天提醒我記錄2330", "記錄2330"),
        ("咪寶：明天提醒我持有2330", "持有2330"),
        ("咪寶：明天提醒我申請1234號", "申請1234號"),
        ("咪寶：明天提醒我準備500元", "準備500元"),
        ("咪寶：明天提醒我寄編號1234的包裹", "寄編號1234的包裹"),
        ("咪寶：明天提醒我領新台幣500元", "領新台幣500元"),
        ("咪寶：明天提醒我取第1234號包裹", "取第1234號包裹"),
        ("咪寶：明天提醒我搭公車520", "搭公車520"),
        ("咪寶：明天提醒我買台積電2330", "買台積電2330"),
    ],
)
def test_direct_single_reminder_parser_does_not_treat_quantities_as_clocks(
    text,
    expected_action,
):
    import main

    parsed = main._explicit_single_reminder_result(
        text,
        "U1",
        now_tw=datetime(2026, 8, 22, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")),
    )

    assert parsed is not None
    assert parsed["action"] == expected_action
    assert (parsed["hour"], parsed["minute"]) == (12, 0)
    assert parsed["_time_default_kind"] == "no_daypart"


def test_today_after_default_time_discloses_five_minute_fallback(monkeypatch):
    import main

    now_tw = datetime(2026, 8, 22, 12, 58, tzinfo=ZoneInfo("Asia/Taipei"))
    parsed = main._explicit_single_reminder_result(
        "咪寶：今天提醒我要領米",
        "U1",
        now_tw=now_tw,
    )

    assert parsed is not None
    assert (parsed["year"], parsed["month"], parsed["day"]) == (2026, 8, 22)
    assert (parsed["hour"], parsed["minute"]) == (13, 3)
    assert parsed["_time_default_kind"] == "five_minutes"
    confirmation = main._format_reminder_write_confirmation(
        "created",
        parsed["action"],
        datetime(2026, 8, 22, 13, 3, tzinfo=ZoneInfo("Asia/Taipei")),
        [],
        parsed["_time_default_kind"],
    )
    assert "已安排 5 分鐘後" in confirmation
    assert "預設 12:00" not in confirmation


def test_today_default_can_roll_to_tomorrow_with_explicit_five_minute_note():
    import main

    parsed = main._explicit_single_reminder_result(
        "咪寶：今天提醒我要領米",
        "U1",
        now_tw=datetime(2026, 8, 22, 23, 58, tzinfo=ZoneInfo("Asia/Taipei")),
    )

    assert parsed is not None
    assert (parsed["year"], parsed["month"], parsed["day"]) == (2026, 8, 23)
    assert (parsed["hour"], parsed["minute"]) == (0, 3)
    assert parsed["_time_default_kind"] == "five_minutes"


@pytest.mark.parametrize(
    "text",
    [
        "咪寶明天1430提醒我領米",
        "咪寶明天提醒我1430領米",
    ],
)
def test_ambiguous_compact_clock_fails_closed_instead_of_writing_wrong_reminder(text):
    import main

    assert (
        main._explicit_single_reminder_result(
            text,
            "U1",
            now_tw=datetime(2026, 8, 22, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")),
        )
        is None
    )


@pytest.mark.parametrize(
    "text",
    [
        "咪寶，明天天氣會怎樣？",
        "咪寶，明天領米嗎？",
        "咪寶，明天要不要領米？",
        "咪寶，明天幾點領米？",
        "咪寶，明天我可以領米",
        "咪寶，明天台積電會漲",
        "咪寶請問明天天氣",
        "咪寶請問明天台鐵",
        "咪寶請問明天股票",
        "咪寶請問明天去哪裡",
        "咪寶請問明天買2330",
        "咪寶明天去不去領米",
        "咪寶明天買不買米",
        "咪寶明天看不看股票",
        "咪寶明天查不查台鐵",
        "咪寶明天去領米好不好",
        "咪寶明天買米行不行",
        "咪寶明天買米可否",
        "咪寶明天買米能否",
        "咪寶明天買哪個",
        "咪寶明天看哪支股票",
        "咪寶明天查哪班火車",
        "咪寶明天去誰家",
        "咪寶明天買多少",
        "咪寶明天買幾個",
        "咪寶明天買幾張票",
        "咪寶，明天",
        "咪寶，提醒我要領米",
        "咪寶，不要提醒我明天領米",
        "咪寶，你已經提醒我明天領米了嗎？",
        "咪寶，媽媽說明天提醒我領米",
        "媽媽說：「咪寶，明天提醒我要領米」",
        "Siri 可以提醒我明天領米嗎？",
        "貓咪寶寶明天領米",
        "咪寶，明天或後天提醒我領米",
        "咪寶，明天下班後提醒我領米",
    ],
)
def test_direct_single_reminder_parser_rejects_ambiguous_or_reported_text(text):
    import main

    assert (
        main._explicit_single_reminder_result(
            text,
            "U1",
            now_tw=datetime(
                2026,
                8,
                22,
                12,
                0,
                tzinfo=ZoneInfo("Asia/Taipei"),
            ),
        )
        is None
    )


def test_explicit_single_reminder_is_persisted_without_gemini_or_pending(
    temp_db,
    monkeypatch,
):
    import gemini_client
    import main
    import memory

    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_a, **_k: False)
    monkeypatch.setattr(
        gemini_client,
        "extract_reminder",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("explicit local reminder must not call Gemini")
        ),
    )
    monkeypatch.setattr(main, "_alias_from_user_id", lambda _uid: "爸爸")

    confirmation = main._maybe_extract_reminder(
        "咪寶：2099/8/23提醒我要領米。",
        "G1",
        "U_DAD",
        "m-local-single",
    )

    with memory._conn() as c:
        reminder = c.execute(
            "SELECT action, remind_at, mention_aliases FROM reminders "
            "WHERE group_id='G1' AND status='pending'"
        ).fetchone()
        pending_count = c.execute(
            "SELECT COUNT(*) FROM pending_reminder_extract WHERE group_id='G1'"
        ).fetchone()[0]
    assert reminder is not None
    assert reminder[0] == "領米"
    assert datetime.fromtimestamp(
        reminder[1], ZoneInfo("Asia/Taipei")
    ).strftime("%Y-%m-%d %H:%M") == "2099-08-23 12:00"
    assert reminder[2] == '["爸爸"]'
    assert pending_count == 0
    assert confirmation == (
        "已新增提醒\n"
        "時間：2099-08-23 12:00（未指定時間，預設 12:00）\n"
        "事項：爸爸 領米"
    )


def test_range_shopping_reminder_is_created_without_gemini(temp_db, monkeypatch):
    """Explicit date-range shopping reminders must not wait for Gemini quota."""
    import gemini_client
    import main
    import memory

    now_tw = datetime(2099, 7, 10, 12, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(
        gemini_client,
        "extract_reminder",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("range reminder should use the local parser")
        ),
    )
    monkeypatch.setattr(
        main,
        "_alias_from_user_id",
        lambda uid: "爸爸" if uid == "U_DAD" else "",
    )
    text = (
        "咪寶：提醒我7月16到7月28日在紐西蘭期間要買：\n"
        "降血糖、血壓的保健食品，按摩油和羊乳片或紐西蘭特產。"
    )
    parsed = main._explicit_range_reminder_result(text, "U_DAD", now_tw=now_tw)
    assert parsed is not None

    confirmation = main._maybe_extract_reminder(
        text,
        "G1",
        "U_DAD",
        "m-range",
        precomputed_result=parsed,
    )

    with memory._conn() as c:
        row = c.execute(
            "SELECT action, remind_at, source_text, mention_aliases "
            "FROM reminders WHERE group_id='G1' AND status='pending'"
        ).fetchone()
    assert row is not None
    remind_dt = datetime.fromtimestamp(row[1], ZoneInfo("Asia/Taipei"))
    assert remind_dt.strftime("%Y-%m-%d %H:%M") == "2099-07-16 12:00"
    assert row[1] == int(
        datetime(2099, 7, 16, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    assert "7/16-7/28" in row[0]
    assert "降血糖、血壓的保健食品" in row[0]
    assert "按摩油" in row[0]
    assert "羊乳片或紐西蘭特產" in row[0]
    assert row[2] == text
    assert row[3] == '["爸爸"]'
    assert confirmation is not None and confirmation.startswith("已新增提醒\n")


def test_repeated_range_reminder_reports_existing_without_duplicate(temp_db, monkeypatch):
    import main
    import memory

    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_a, **_k: False)
    monkeypatch.setattr(main, "_alias_from_user_id", lambda uid: "爸爸")
    text = "咪寶：提醒我7月16到7月28日在紐西蘭期間要買：按摩油。"
    parsed = main._explicit_range_reminder_result(
        text,
        "U_DAD",
        now_tw=datetime(2099, 7, 10, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")),
    )
    assert parsed is not None

    first = main._maybe_extract_reminder(
        text, "G1", "U_DAD", "m-range", precomputed_result=parsed
    )
    second = main._maybe_extract_reminder(
        text, "G1", "U_DAD", "m-range-redelivery", precomputed_result=parsed
    )

    with memory._conn() as c:
        count = c.execute(
            "SELECT COUNT(*) FROM reminders WHERE group_id='G1' AND status='pending'"
        ).fetchone()[0]
    assert first is not None and first.startswith("已新增提醒\n")
    assert second is not None and second.startswith("提醒已存在，未重複新增\n")
    assert count == 1


def test_range_reminder_parser_handles_cross_year_time_and_invalid_dates(monkeypatch):
    import main

    monkeypatch.setattr(main, "_alias_from_user_id", lambda uid: "爸爸")
    now = datetime(2026, 7, 10, 12, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    cross_year = main._explicit_range_reminder_result(
        "提醒我12月28到1月5日在紐西蘭期間要買：按摩油",
        "U_DAD",
        now_tw=now,
    )
    explicit_time = main._explicit_range_reminder_result(
        "提醒我7月16至7月28日下午3點在紐西蘭期間要買：按摩油",
        "U_DAD",
        now_tw=now,
    )

    assert cross_year is not None
    assert (cross_year["year"], cross_year["month"], cross_year["day"]) == (
        2026,
        12,
        28,
    )
    assert cross_year["range_end"] == "2027-01-05"
    assert (cross_year["hour"], cross_year["minute"]) == (12, 0)
    assert explicit_time is not None
    assert (explicit_time["hour"], explicit_time["minute"]) == (15, 0)
    item_time = main._explicit_range_reminder_result(
        "提醒我7月16至7月28日在紐西蘭期間要買：下午3點按摩油。對象：@爸爸和媽媽",
        "U_DAD",
        now_tw=now,
    )
    assert item_time is not None
    assert (item_time["hour"], item_time["minute"]) == (12, 0)
    assert "對象" not in item_time["action"]
    assert item_time["mention_aliases"] == ["爸爸", "媽媽"]
    english_item = main._explicit_range_reminder_result(
        "提醒我7月16至7月28日在紐西蘭期間要買：Manuka Honey UMF 15+",
        "U_DAD",
        now_tw=now,
    )
    assert english_item is not None
    assert "Manuka Honey UMF 15+" in english_item["action"]
    assert main._explicit_range_reminder_result(
        "提醒我2月30到3月2日期間要買：按摩油",
        "U_DAD",
        now_tw=now,
    ) is None
    assert main._explicit_range_reminder_result(
        "提醒我2025年7月16到2025年7月28日期間要買：按摩油",
        "U_DAD",
        now_tw=now,
    ) is None


def test_duplicate_confirmation_uses_canonical_saved_time(temp_db, monkeypatch):
    import main

    future = datetime.now(ZoneInfo("Asia/Taipei")) + timedelta(days=2)
    base = {
        "action": "幫爸爸買按摩油",
        "year": future.year,
        "month": future.month,
        "day": future.day,
        "hour": 10,
        "minute": 0,
        "mention_aliases": ["爸爸"],
    }
    later = {**base, "minute": 30}

    first = main._maybe_extract_reminder(
        "後天提醒幫爸爸買按摩油", "G1", "U1", "m1", precomputed_result=base
    )
    duplicate = main._maybe_extract_reminder(
        "後天提醒幫爸爸買按摩油", "G1", "U1", "m2", precomputed_result=later
    )

    assert first is not None and "時間：" + future.strftime("%Y-%m-%d") + " 10:00" in first
    assert duplicate is not None and duplicate.startswith("提醒已存在，未重複新增\n")
    assert " 10:00\n" in duplicate
    assert " 10:30\n" not in duplicate


def test_maybe_extract_subjectless_medical_uses_sender_alias(temp_db, monkeypatch):
    """Subjectless medical reminders should include who from sender alias."""
    import main
    import memory
    import gemini_client

    monkeypatch.setattr(gemini_client, "extract_reminder", lambda *a, **k: None)
    monkeypatch.setattr(
        main,
        "_alias_from_user_id",
        lambda uid: "媽媽" if uid == "U_MOM" else "",
    )

    main._maybe_extract_reminder(
        "明天早上十點半看台大測試牙醫乙", "G1", "U_MOM", "m1"
    )

    with memory._conn() as c:
        row = c.execute(
            "SELECT action FROM reminders WHERE group_id='G1' AND status='pending'"
        ).fetchone()
    assert row is not None
    assert row[0] == "媽媽看台大測試牙醫乙"


def test_maybe_extract_rewrites_first_person_action_to_sender_alias(temp_db, monkeypatch):
    """Gemini may return first-person action; store concrete actor instead."""
    import main
    import memory
    import gemini_client

    future = datetime.now() + timedelta(days=2)
    monkeypatch.setattr(
        gemini_client,
        "extract_reminder",
        lambda *a, **k: {
            "action": "我看台大測試牙醫乙",
            "year": future.year,
            "month": future.month,
            "day": future.day,
            "hour": 10,
            "minute": 30,
        },
    )
    monkeypatch.setattr(
        main,
        "_alias_from_user_id",
        lambda uid: "媽媽" if uid == "U_MOM" else "",
    )

    main._maybe_extract_reminder(
        "星期四早上十點半看台大測試牙醫乙", "G1", "U_MOM", "m1"
    )

    with memory._conn() as c:
        row = c.execute(
            "SELECT action FROM reminders WHERE group_id='G1' AND status='pending'"
        ).fetchone()
    assert row is not None
    assert row[0] == "媽媽看台大測試牙醫乙"


def test_maybe_extract_medical_prep_inherits_patient_and_companion(temp_db, monkeypatch):
    """Medical prep reminders must keep patient and companion roles from context."""
    import main
    import memory
    import gemini_client

    future = datetime.now() + timedelta(days=2)
    action = "正子斷層掃描當天 08:00 開始禁食 6 小時，只能喝水"
    monkeypatch.setattr(
        gemini_client,
        "extract_reminder",
        lambda *a, **k: {
            "action": action,
            "year": future.year,
            "month": future.month,
            "day": future.day,
            "hour": 8,
            "minute": 0,
        },
    )
    monkeypatch.setattr(
        main,
        "_alias_from_user_id",
        lambda uid: "媽媽" if uid == "U_MOM" else "",
    )
    monkeypatch.setattr(
        main,
        "_FAMILY_ACTOR_TERMS",
        (*main._FAMILY_ACTOR_TERMS, "測試成員甲"),
    )

    text = (
        f"{future.month}月{future.day}日星期二下午兩點前要到台大醫院東址地下1樓"
        "做正子斷層掃描。測試成員甲要陪我去，大約需要兩個多小時。"
        "當天早上8:00開始禁食 6小時。只能喝水。"
    )
    main._maybe_extract_reminder(text, "G1", "U_MOM", "m1")

    with memory._conn() as c:
        row = c.execute(
            "SELECT action, mention_aliases FROM reminders "
            "WHERE group_id='G1' AND status='pending'"
        ).fetchone()
    assert row is not None
    assert row[0] == f"媽媽{action}（測試成員甲陪同）"
    assert row[1] == '["媽媽", "測試成員甲"]'


def _future_tuesday_before_thursday() -> tuple[datetime, datetime]:
    """Return a future Tuesday message time and its Thursday 10:30 target."""
    target = datetime.now() + timedelta(days=1)
    while target.weekday() != 3:  # Thursday
        target += timedelta(days=1)
    target = target.replace(hour=10, minute=30, second=0, microsecond=0)
    msg_time = (target - timedelta(days=2)).replace(hour=9, minute=0)
    return msg_time, target


# ── drain: R1 相對日期 ────────────────────────────────────────────────────────

def test_drain_relative_date_uses_message_created_at(temp_db, monkeypatch):
    """R1（GP1 critical）: drain 重抽「明天」要用訊息當時 created_at，不是 drain 當天。"""
    import main
    import memory
    import gemini_client
    # 真實情境：訊息昨天進來（quota 爆），今天額度恢復 drain。「明天」必須對到
    # 訊息當天的明天，不是 drain 當天的明天。用昨天（< 7 天 stale 閾值，不被 drop）。
    yesterday = datetime.now() - timedelta(days=1)
    memory.enqueue_pending_reminder("G1", "U1", "明天早上9點開會", "m1")
    with memory._conn() as c:
        c.execute("UPDATE pending_reminder_extract SET created_at=? WHERE message_id='m1'",
                  (int(yesterday.timestamp()),))
    captured = {}

    def fake_extract(text, today_iso=None):
        captured["today_iso"] = today_iso
        return None

    monkeypatch.setattr(gemini_client, "extract_reminder", fake_extract)
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)
    main._drain_pending_reminders("G1")
    expected = yesterday.strftime("%Y-%m-%d")
    assert captured.get("today_iso", "").startswith(expected), \
        f"R1 broken: today_iso={captured.get('today_iso')!r} 應為訊息當天 {expected}（不是今天）"


def test_drain_relative_date_uses_taiwan_day_at_midnight_boundary(temp_db, monkeypatch):
    import gemini_client
    import main
    import memory

    message_time_tw = datetime(
        2026, 7, 10, 0, 30, tzinfo=ZoneInfo("Asia/Taipei")
    )
    memory.enqueue_pending_reminder("G1", "U1", "明天早上9點開會", "m-midnight")
    with memory._conn() as c:
        c.execute(
            "UPDATE pending_reminder_extract SET created_at=? WHERE message_id='m-midnight'",
            (int(message_time_tw.timestamp()),),
        )
    captured = {}

    def fake_extract(text, today_iso=None):
        captured["today_iso"] = today_iso
        return {
            "action": "開會",
            "year": 2099,
            "month": 7,
            "day": 11,
            "hour": 9,
            "minute": 0,
        }

    monkeypatch.setattr(gemini_client, "extract_reminder", fake_extract)
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)
    monkeypatch.setattr(memory, "drop_stale_pending_reminders", lambda *_a, **_k: 0)

    main._drain_pending_reminders("G1")

    assert captured["today_iso"].startswith("2026-07-10")


# ── drain: 成功 / dropped / 過期 ─────────────────────────────────────────────

def test_drain_success_adds_reminder(temp_db, monkeypatch):
    import main
    import memory
    import gemini_client
    future = datetime.now() + timedelta(days=3)
    memory.enqueue_pending_reminder("G1", "U1", "3天後下午8點開會", "m1")
    monkeypatch.setattr(gemini_client, "extract_reminder", lambda *a, **k: {
        "action": "開會", "year": future.year, "month": future.month,
        "day": future.day, "hour": 20, "minute": 0,
    })
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)
    main._drain_pending_reminders("G1")
    assert memory.list_pending_reminder_retries("G1") == [], "成功後應離開 pending"
    # reminders 表應有一筆
    with memory._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM reminders WHERE action='開會'").fetchone()[0]
        confirmation = c.execute(
            "SELECT text, status FROM reminder_confirmation_outbox WHERE group_id='G1'"
        ).fetchone()
    assert n == 1, "drain 成功應寫進 reminders"
    # 2026-10-04: a late extraction sends no receipt; its next push is the signal.
    assert confirmation is None


def test_drain_add_reminder_failure_releases_claim(temp_db, monkeypatch):
    """After claim, DB/write failures must not strand the row in processing."""
    import main
    import memory
    import gemini_client
    future = datetime.now() + timedelta(days=3)
    memory.enqueue_pending_reminder("G1", "U1", "3天後下午8點開會", "m1")
    monkeypatch.setattr(gemini_client, "extract_reminder", lambda *a, **k: {
        "action": "開會", "year": future.year, "month": future.month,
        "day": future.day, "hour": 20, "minute": 0,
    })
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)
    monkeypatch.setattr(
        memory,
        "complete_pending_reminder",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db locked")),
    )

    main._drain_pending_reminders("G1")

    with memory._conn() as c:
        status, retries = c.execute(
            "SELECT status, retries FROM pending_reminder_extract WHERE message_id='m1'"
        ).fetchone()
    assert status == "pending"
    assert retries == 1


def test_processing_claim_reclaimed_after_worker_crash(temp_db):
    """A killed worker must not leave reminder rows invisible until 7d stale drop."""
    import time
    import memory

    pid = memory.enqueue_pending_reminder("G1", "U1", "3天後下午8點開會", "m1")
    assert pid is not None
    first_claim_token = memory.claim_pending_reminder(pid)
    assert first_claim_token
    old_claim = int(time.time()) - 1200
    with memory._conn() as c:
        c.execute(
            "UPDATE pending_reminder_extract SET claimed_at=? WHERE pending_id=?",
            (old_claim, pid),
        )

    rows = memory.list_pending_reminder_retries("G1")

    assert [r["pending_id"] for r in rows] == [pid]
    with memory._conn() as c:
        status, retries, claimed_at = c.execute(
            "SELECT status, retries, claimed_at FROM pending_reminder_extract "
            "WHERE pending_id=?",
            (pid,),
        ).fetchone()
    assert status == "pending"
    assert retries == 1
    assert claimed_at == 0
    second_claim_token = memory.claim_pending_reminder(pid)
    assert second_claim_token and second_claim_token != first_claim_token
    assert not memory.release_pending_reminder(pid, first_claim_token)
    assert not memory.mark_pending_reminder(pid, "done", first_claim_token)
    assert memory.release_pending_reminder(pid, second_claim_token)


def test_drain_none_drops(temp_db, monkeypatch):
    """Gemini 判定非提醒(None) → dropped，不無限重抽。"""
    import main
    import memory
    import gemini_client
    memory.enqueue_pending_reminder("G1", "U1", "6/3下午8點開會", "m1")
    monkeypatch.setattr(gemini_client, "extract_reminder", lambda *a, **k: None)
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)
    main._drain_pending_reminders("G1")
    assert memory.list_pending_reminder_retries("G1") == [], "None 應 dropped 離開 pending"
    with memory._conn() as c:
        st, reason = c.execute(
            "SELECT status, drop_reason FROM pending_reminder_extract WHERE message_id='m1'"
        ).fetchone()
        confirmation = c.execute(
            "SELECT text, status FROM reminder_confirmation_outbox "
            "WHERE group_id='G1'"
        ).fetchone()
    assert st == "dropped"
    assert reason == "model_null"
    assert confirmation is None


def test_drop_pending_reminder_is_claim_guarded_and_idempotent(temp_db):
    import memory

    pending_id = memory.enqueue_pending_reminder(
        "G1", "U1", "明天提醒我處理事情", "m-atomic-drop"
    )
    assert pending_id is not None
    claim_token = memory.claim_pending_reminder(pending_id)
    assert claim_token

    assert memory.drop_pending_reminder(pending_id, claim_token, "G1", "model_null")
    assert not memory.drop_pending_reminder(pending_id, claim_token, "G1", "model_null")

    with memory._conn() as c:
        pending = c.execute(
            "SELECT status, claimed_at, claim_token, drop_reason "
            "FROM pending_reminder_extract WHERE pending_id=?",
            (pending_id,),
        ).fetchone()
        receipts = c.execute(
            "SELECT COUNT(*) FROM reminder_confirmation_outbox"
        ).fetchone()[0]
    assert pending == ("dropped", 0, "", "model_null")
    assert receipts == 0


def test_drop_pending_reminder_never_writes_the_outbox(temp_db):
    import memory

    pending_id = memory.enqueue_pending_reminder(
        "G1", "U1", "明天提醒我處理事情", "m-rollback-drop"
    )
    assert pending_id is not None
    claim_token = memory.claim_pending_reminder(pending_id)
    assert claim_token
    with memory._conn() as c:
        c.execute(
            "CREATE TRIGGER fail_terminal_receipt BEFORE INSERT "
            "ON reminder_confirmation_outbox BEGIN "
            "SELECT RAISE(ABORT, 'forced receipt failure'); END"
        )

    assert memory.drop_pending_reminder(pending_id, claim_token, "G1", "expired")

    with memory._conn() as c:
        status, stored_claim = c.execute(
            "SELECT status, claim_token FROM pending_reminder_extract "
            "WHERE pending_id=?",
            (pending_id,),
        ).fetchone()
    assert (status, stored_claim) == ("dropped", "")


def test_concurrent_drop_closes_the_row_exactly_once(temp_db):
    import memory

    pending_id = memory.enqueue_pending_reminder(
        "G1", "U1", "明天提醒我處理事情", "m-concurrent-drop"
    )
    assert pending_id is not None
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(processes=6) as pool:
        results = pool.map(
            _concurrent_drop_pending_worker,
            [(str(temp_db), pending_id, "model_null")] * 18,
        )

    assert results.count(True) == 1
    with memory._conn() as c:
        status = c.execute(
            "SELECT status FROM pending_reminder_extract WHERE pending_id=?",
            (pending_id,),
        ).fetchone()[0]
        receipts = c.execute(
            "SELECT COUNT(*) FROM reminder_confirmation_outbox"
        ).fetchone()[0]
    assert status == "dropped"
    assert receipts == 0


def test_stale_pending_cleanup_is_silent_and_group_scoped(temp_db):
    import time
    import memory

    now = int(time.time())
    old_g1 = memory.enqueue_pending_reminder("G1", "U1", "舊候選", "m-old-g1")
    fresh_g1 = memory.enqueue_pending_reminder("G1", "U1", "新候選", "m-fresh-g1")
    old_g2 = memory.enqueue_pending_reminder("G2", "U2", "別群舊候選", "m-old-g2")
    already_dropped = memory.enqueue_pending_reminder(
        "G1", "U1", "歷史已 dropped", "m-historical-dropped"
    )
    assert all((old_g1, fresh_g1, old_g2, already_dropped))
    with memory._conn() as c:
        c.execute(
            "UPDATE pending_reminder_extract SET status='dropped' "
            "WHERE pending_id=?",
            (already_dropped,),
        )
        c.execute(
            "UPDATE pending_reminder_extract SET created_at=? "
            "WHERE pending_id IN (?, ?, ?)",
            (now - 7200, old_g1, old_g2, already_dropped),
        )

    assert memory.drop_stale_pending_reminders(3600, "G1") == 1
    assert memory.drop_stale_pending_reminders(3600, "G1") == 0

    with memory._conn() as c:
        statuses = dict(
            c.execute(
                "SELECT pending_id, status FROM pending_reminder_extract"
            ).fetchall()
        )
        reasons = dict(
            c.execute(
                "SELECT pending_id, drop_reason FROM pending_reminder_extract"
            ).fetchall()
        )
        receipts = c.execute(
            "SELECT COUNT(*) FROM reminder_confirmation_outbox"
        ).fetchone()[0]
    assert statuses[old_g1] == "dropped" and reasons[old_g1] == "stale"
    assert statuses[fresh_g1] == "pending"
    assert statuses[old_g2] == "pending"
    assert statuses[already_dropped] == "dropped"
    assert receipts == 0


def test_stale_pending_cleanup_never_writes_the_outbox(temp_db):
    import time
    import memory

    now = int(time.time())
    first_id = memory.enqueue_pending_reminder("G1", "U1", "舊候選一", "m-old-1")
    second_id = memory.enqueue_pending_reminder("G1", "U1", "舊候選二", "m-old-2")
    assert first_id and second_id
    with memory._conn() as c:
        c.execute(
            "UPDATE pending_reminder_extract SET created_at=? "
            "WHERE pending_id IN (?, ?)",
            (now - 7200, first_id, second_id),
        )
        c.execute(
            "CREATE TRIGGER fail_any_stale_receipt BEFORE INSERT "
            "ON reminder_confirmation_outbox BEGIN "
            "SELECT RAISE(ABORT, 'forced receipt failure'); END"
        )

    assert memory.drop_stale_pending_reminders(3600, "G1") == 2

    with memory._conn() as c:
        statuses = c.execute(
            "SELECT status, drop_reason FROM pending_reminder_extract "
            "WHERE pending_id IN (?, ?) ORDER BY pending_id",
            (first_id, second_id),
        ).fetchall()
    assert statuses == [("dropped", "stale"), ("dropped", "stale")]


def test_generic_pending_marker_rejects_unclassified_drop(temp_db):
    import memory

    pending_id = memory.enqueue_pending_reminder(
        "G1", "U1", "明天提醒我處理事情", "m-unclassified-drop"
    )
    assert pending_id is not None
    claim_token = memory.claim_pending_reminder(pending_id)
    assert claim_token

    with pytest.raises(ValueError, match="explicit pending reminder API"):
        memory.mark_pending_reminder(pending_id, "dropped", claim_token)

    assert memory.release_pending_reminder(pending_id, claim_token)


def test_drain_none_falls_back_to_calendar_regex_with_message_date(temp_db, monkeypatch):
    """Pending drain 的 None 也要用訊息 created_at 解「星期四」。"""
    import main
    import memory
    import gemini_client

    memory.enqueue_pending_reminder(
        "G1", "U1", "星期四早上十點半看台大測試牙醫乙", "m1"
    )
    msg_time, expected = _future_tuesday_before_thursday()
    with memory._conn() as c:
        c.execute(
            "UPDATE pending_reminder_extract SET created_at=? WHERE message_id='m1'",
            (int(msg_time.timestamp()),),
        )
    monkeypatch.setattr(gemini_client, "extract_reminder", lambda *a, **k: None)
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)

    main._drain_pending_reminders("G1")

    with memory._conn() as c:
        row = c.execute(
            "SELECT action, remind_at "
            "FROM reminders WHERE group_id='G1'"
        ).fetchone()
        status = c.execute(
            "SELECT status FROM pending_reminder_extract WHERE message_id='m1'"
        ).fetchone()[0]
    assert row is not None
    assert row[0] == "看台大測試牙醫乙"
    assert datetime.fromtimestamp(row[1]).strftime("%Y-%m-%d %H:%M") == (
        expected.strftime("%Y-%m-%d %H:%M")
    )
    assert status == "done"


def test_drain_subjectless_medical_uses_sender_alias(temp_db, monkeypatch):
    """Pending drain should preserve sender identity when Gemini returns None."""
    import main
    import memory
    import gemini_client

    memory.enqueue_pending_reminder(
        "G1", "U_MOM", "星期四早上十點半看台大測試牙醫乙", "m1"
    )
    msg_time, _expected = _future_tuesday_before_thursday()
    with memory._conn() as c:
        c.execute(
            "UPDATE pending_reminder_extract SET created_at=? WHERE message_id='m1'",
            (int(msg_time.timestamp()),),
        )
    monkeypatch.setattr(gemini_client, "extract_reminder", lambda *a, **k: None)
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)
    monkeypatch.setattr(main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else "")

    main._drain_pending_reminders("G1")

    with memory._conn() as c:
        row = c.execute(
            "SELECT action FROM reminders WHERE group_id='G1' AND status='pending'"
        ).fetchone()
    assert row is not None
    assert row[0] == "媽媽看台大測試牙醫乙"


def test_drain_expired_drops(temp_db, monkeypatch):
    """抽出來的時間已過期 → dropped，不寫進 reminders。"""
    import main
    import memory
    import gemini_client
    past = datetime.now() - timedelta(days=2)
    memory.enqueue_pending_reminder("G1", "U1", "前天8點", "m1")
    monkeypatch.setattr(gemini_client, "extract_reminder", lambda *a, **k: {
        "action": "過期事件", "year": past.year, "month": past.month,
        "day": past.day, "hour": 20, "minute": 0,
    })
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)
    main._drain_pending_reminders("G1")
    with memory._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM reminders WHERE action='過期事件'").fetchone()[0]
        st, reason = c.execute(
            "SELECT status, drop_reason FROM pending_reminder_extract WHERE message_id='m1'"
        ).fetchone()
        confirmation = c.execute(
            "SELECT text, status FROM reminder_confirmation_outbox "
            "WHERE group_id='G1'"
        ).fetchone()
    assert n == 0 and st == "dropped"
    assert reason == "expired"
    assert confirmation is None


def test_drain_invalid_datetime_drops_silently(temp_db, monkeypatch):
    import main
    import memory
    import gemini_client

    memory.enqueue_pending_reminder("G1", "U1", "明天下午開會", "m-invalid")
    monkeypatch.setattr(gemini_client, "extract_reminder", lambda *_a, **_k: {
        "action": "開會",
        "year": 2099,
        "month": 1,
        "day": 1,
        "hour": 25,
        "minute": 0,
    })
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)

    main._drain_pending_reminders("G1")

    with memory._conn() as c:
        status, reason = c.execute(
            "SELECT status, drop_reason FROM pending_reminder_extract "
            "WHERE message_id='m-invalid'"
        ).fetchone()
        confirmation = c.execute(
            "SELECT text, status FROM reminder_confirmation_outbox "
            "WHERE group_id='G1'"
        ).fetchone()
    assert status == "dropped"
    assert reason == "no_date"
    assert confirmation is None


# ── drain: quota gate + release ──────────────────────────────────────────────

def test_drain_quota_gate_skips(temp_db, monkeypatch):
    """額度仍爆 → drain 完全不動（不浪費 API、不誤標）。"""
    import main
    import memory
    import gemini_client
    memory.enqueue_pending_reminder("G1", "U1", "6/3下午8點開會", "m1")
    called = []
    monkeypatch.setattr(gemini_client, "extract_reminder",
                        lambda *a, **k: called.append(1))
    monkeypatch.setattr(main, "_quota_exhausted", lambda: True)  # 仍爆
    main._drain_pending_reminders("G1")
    assert called == [], "quota 爆時不可呼叫 extract_reminder"
    assert len(memory.list_pending_reminder_retries("G1")) == 1, "pending 保留"


def test_drain_processes_local_range_reminder_while_quota_is_exhausted(
    temp_db, monkeypatch
):
    import gemini_client
    import main
    import memory

    text = "咪寶：提醒我7月16到7月28日在紐西蘭期間要買：按摩油。"
    memory.enqueue_pending_reminder("G1", "U_DAD", text, "m-range")
    monkeypatch.setattr(main, "_quota_exhausted", lambda: True)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: False)
    monkeypatch.setattr(main, "_alias_from_user_id", lambda uid: "爸爸")
    monkeypatch.setattr(
        gemini_client,
        "extract_reminder",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("local range drain must not call Gemini")
        ),
    )

    main._drain_pending_reminders("G1")

    assert memory.list_pending_reminder_retries("G1") == []
    with memory._conn() as c:
        reminder = c.execute(
            "SELECT action, mention_aliases FROM reminders WHERE group_id='G1'"
        ).fetchone()
        confirmation = c.execute(
            "SELECT text FROM reminder_confirmation_outbox WHERE group_id='G1'"
        ).fetchone()
    assert reminder is not None and "7/16-7/28" in reminder[0]
    assert reminder[1] == '["爸爸"]'
    assert confirmation is None  # late extraction: no receipt (2026-10-04)


def test_drain_processes_explicit_single_reminder_while_quota_is_exhausted(
    temp_db,
    monkeypatch,
):
    import gemini_client
    import main
    import memory

    now_tw = datetime.now(ZoneInfo("Asia/Taipei"))
    text = "咪寶：明天提醒我要領米。"
    pending_id = memory.enqueue_pending_reminder(
        "G1", "U_DAD", text, "m-local-single"
    )
    assert pending_id is not None
    monkeypatch.setattr(main, "_quota_exhausted", lambda: True)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: False)
    monkeypatch.setattr(main, "_alias_from_user_id", lambda _uid: "爸爸")
    monkeypatch.setattr(
        gemini_client,
        "extract_reminder",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("local-only pending drain must not call Gemini")
        ),
    )

    main._drain_pending_reminders("G1", local_only=True)

    with memory._conn() as c:
        pending_status = c.execute(
            "SELECT status FROM pending_reminder_extract WHERE pending_id=?",
            (pending_id,),
        ).fetchone()[0]
        reminder = c.execute(
            "SELECT action, remind_at, mention_aliases FROM reminders "
            "WHERE group_id='G1'"
        ).fetchone()
        confirmation = c.execute(
            "SELECT text FROM reminder_confirmation_outbox "
            "WHERE source_ref=?",
            (f"pending_reminder:{pending_id}",),
        ).fetchone()
    assert pending_status == "done"
    assert reminder is not None and reminder[0] == "領米"
    assert datetime.fromtimestamp(
        reminder[1], ZoneInfo("Asia/Taipei")
    ).strftime("%Y-%m-%d %H:%M") == (
        (now_tw + timedelta(days=1)).strftime("%Y-%m-%d") + " 12:00"
    )
    assert reminder[2] == '["爸爸"]'
    assert confirmation is None  # late extraction: no receipt (2026-10-04)


def test_local_only_drain_scans_past_remote_rows_without_dropping_them(
    temp_db,
    monkeypatch,
):
    import gemini_client
    import main
    import memory

    first_created = int(datetime.now(ZoneInfo("Asia/Taipei")).timestamp()) - 60
    remote_ids = []
    for index in range(55):
        pending_id = memory.enqueue_pending_reminder(
            "G1",
            "U1",
            f"明天去第{index + 1}個地方",
            f"m-remote-{index}",
        )
        remote_ids.append(pending_id)
    local_id = memory.enqueue_pending_reminder(
        "G1",
        "U_DAD",
        "咪寶：明天提醒我要領米。",
        "m-local-after-fifty-five",
    )
    with memory._conn() as c:
        for offset, pending_id in enumerate([*remote_ids, local_id]):
            c.execute(
                "UPDATE pending_reminder_extract SET created_at=? WHERE pending_id=?",
                (first_created + offset, pending_id),
            )
    monkeypatch.setattr(main, "_quota_exhausted", lambda: True)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: False)
    monkeypatch.setattr(main, "_alias_from_user_id", lambda _uid: "爸爸")
    monkeypatch.setattr(
        gemini_client,
        "extract_reminder",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("local-only scan must not call Gemini")
        ),
    )

    main._drain_pending_reminders("G1", limit=1, local_only=True)

    with memory._conn() as c:
        remote_statuses = [
            c.execute(
                "SELECT status FROM pending_reminder_extract WHERE pending_id=?",
                (pending_id,),
            ).fetchone()[0]
            for pending_id in remote_ids
        ]
        local_status = c.execute(
            "SELECT status FROM pending_reminder_extract WHERE pending_id=?",
            (local_id,),
        ).fetchone()[0]
    assert remote_statuses == ["pending"] * 55
    assert local_status == "done"


def test_drain_429_releases_and_stops(temp_db, monkeypatch):
    """drain 中途又撞日額度 429 → release（退回 pending）+ 停本輪。"""
    import main
    import memory
    import gemini_client
    memory.enqueue_pending_reminder("G1", "U1", "6/3下午8點甲", "m1")
    memory.enqueue_pending_reminder("G1", "U1", "6/4下午8點乙", "m2")
    monkeypatch.setattr(gemini_client, "extract_reminder",
                        lambda *a, **k: (_ for _ in ()).throw(_quota_429_perday()))
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)
    monkeypatch.setattr(main, "_mark_quota_exhausted", lambda: None)
    main._drain_pending_reminders("G1")
    # 兩筆都該還在 pending（第一筆 release 退回、第二筆因 break 沒被碰）
    assert len(memory.list_pending_reminder_retries("G1")) == 2, "429 後兩筆都應保留"


def test_drain_quota_usage_over_60pct_skips(temp_db, monkeypatch):
    """reserve gate（用量>60%）→ 不抽，保額度給新訊息（GP2 D1）。"""
    import main
    import memory
    import gemini_client
    memory.enqueue_pending_reminder("G1", "U1", "6/3下午8點開會", "m1")
    called = []
    monkeypatch.setattr(gemini_client, "extract_reminder", lambda *a, **k: called.append(1))
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: False)  # 用量>60%
    main._drain_pending_reminders("G1")
    assert called == [], "reserve gate 應擋下 drain"
    assert len(memory.list_pending_reminder_retries("G1")) == 1


# ── gemini_client.extract_reminder 真實 429 行為（Phase6 GP-A IMPORTANT-2）─────

def test_extract_reminder_reraises_perday_429(monkeypatch):
    """R3 基石：extract_reminder 撞真實 429 PerDay 應 bare raise 保留原字串，不靜默
    回 None。monkeypatch generate_content（非整個 extract_reminder）測真實偵測邏輯。"""
    import gemini_client

    class _FakeModels:
        def generate_content(self, **kw):
            raise Exception(
                "429 RESOURCE_EXHAUSTED. Quota exceeded for metric: "
                "generate_content_free_tier_requests ... PerDay ... limit: 20"
            )

    class _FakeClient:
        models = _FakeModels()

    monkeypatch.setattr(gemini_client, "_client", _FakeClient())
    monkeypatch.setattr(gemini_client, "_track_failed_request", lambda: None)
    with pytest.raises(Exception) as ei:
        gemini_client.extract_reminder("6/3下午8點開會")
    s = str(ei.value)
    assert "429" in s and "free_tier_requests" in s, \
        f"原始 429 字串應保留供下游 _is_quota_error 判 PerDay: {s}"


def test_extract_reminder_non_429_api_error_raises(monkeypatch):
    """2026-10-04 介面約定：None 只代表模型判定不是提醒；模型無法使用（含非 429
    的 API／傳輸錯誤）一律 raise，呼叫端才不會把「模型沒回」當成「模型說不是」。"""
    import gemini_client

    class _FakeModels:
        def generate_content(self, **kw):
            raise Exception("some unrelated transport error")

    class _FakeClient:
        models = _FakeModels()

    monkeypatch.setattr(gemini_client, "_client", _FakeClient())
    with pytest.raises(Exception, match="unrelated transport error"):
        gemini_client.extract_reminder("6/3下午8點開會")


def test_extract_reminder_unreadable_answer_returns_none(monkeypatch):
    """模型有回但內容不是 JSON：視為沒有可用的提醒，回 None。"""
    import gemini_client
    from types import SimpleNamespace

    class _FakeModels:
        def generate_content(self, **kw):
            return SimpleNamespace(text="這不是 JSON", usage_metadata=None)

    class _FakeClient:
        models = _FakeModels()

    monkeypatch.setattr(gemini_client, "_client", _FakeClient())
    assert gemini_client.extract_reminder("6/3下午8點開會") is None


def test_add_reminder_normalizes_common_asr_errors(temp_db):
    """Common ASR/OCR slips should not be persisted into reminder actions."""
    import memory

    future = datetime.now() + timedelta(days=1)

    memory.add_reminder(
        "G1",
        "U1",
        "去教會4樓參加嗎？那小組的茶几",
        int(future.timestamp()),
        source_text="去教會4樓參加嗎？那小組的茶几",
    )

    with memory._conn() as c:
        action, source_text = c.execute(
            "SELECT action, source_text FROM reminders WHERE group_id='G1'"
        ).fetchone()
    assert action == "去教會4樓參加嗎哪小組的查經"
    assert source_text == "去教會4樓參加嗎哪小組的查經"


def test_add_reminder_persists_structured_mention_aliases(temp_db):
    import memory

    future = datetime.now() + timedelta(days=1)

    memory.add_reminder(
        "G1",
        "U_MOM",
        "正子斷層掃描當天 08:00 開始禁食",
        int(future.timestamp()),
        mention_aliases=["媽媽", "測試成員甲"],
    )

    rows = memory.list_pending_reminders_full("G1")

    assert len(rows) == 1
    assert rows[0]["mention_aliases"] == ["媽媽", "測試成員甲"]


def test_add_reminder_with_outcome_distinguishes_create_duplicate_and_merge(temp_db):
    import memory

    remind_at = int((datetime.now() + timedelta(days=1)).timestamp())
    rid, outcome = memory.add_reminder_with_outcome(
        "G1",
        "U1",
        "媽媽行程：嗎哪小組（19:15-21:30）",
        remind_at,
        source_text="媽媽排程圖片：嗎哪小組",
    )
    duplicate_id, duplicate_outcome = memory.add_reminder_with_outcome(
        "G1",
        "U1",
        "媽媽行程：嗎哪小組（19:15-21:30）",
        remind_at,
        source_text="媽媽排程圖片：嗎哪小組",
    )
    merged_id, merged_outcome = memory.add_reminder_with_outcome(
        "G1",
        "U1",
        "去教會4樓參加嗎？那小組的茶几",
        remind_at,
        source_text="明天晚上去教會4樓參加嗎？那小組的茶几",
    )

    assert outcome == "created"
    assert duplicate_outcome == "duplicate"
    assert merged_outcome == "merged"
    assert rid == duplicate_id == merged_id


def test_add_reminder_dedup_is_atomic_across_processes(temp_db):
    import memory

    remind_at = int((datetime.now() + timedelta(days=1)).timestamp())
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(processes=6) as pool:
        results = pool.map(
            _concurrent_add_reminder_worker,
            [(str(temp_db), remind_at)] * 24,
        )

    with memory._conn() as c:
        count = c.execute(
            "SELECT COUNT(*) FROM reminders WHERE group_id='G1' "
            "AND action='同一筆跨程序提醒'"
        ).fetchone()[0]
    assert count == 1
    assert [outcome for _rid, outcome in results].count("created") == 1


def test_reminder_confirmation_outbox_claim_release_and_commit(temp_db):
    import memory

    first = memory.enqueue_reminder_confirmation("G1", "pending:1", "第一筆")
    second = memory.enqueue_reminder_confirmation("G1", "pending:2", "第二筆")
    duplicate = memory.enqueue_reminder_confirmation("G1", "pending:1", "不應覆寫")

    assert first and second and duplicate == first
    claimed = memory.claim_reminder_confirmations("G1", limit=4)
    assert [row["text"] for row in claimed] == ["第一筆", "第二筆"]
    assert memory.claim_reminder_confirmations("G1", limit=4) == []

    claims = [
        (row["confirmation_id"], row["claim_token"])
        for row in claimed
    ]
    assert memory.release_reminder_confirmations("G1", claims) == 2
    reclaimed = memory.claim_reminder_confirmations("G1", limit=4)
    assert [row["confirmation_id"] for row in reclaimed] == [
        confirmation_id for confirmation_id, _token in claims
    ]
    reclaimed_claims = [
        (row["confirmation_id"], row["claim_token"])
        for row in reclaimed
    ]
    assert memory.delete_sent_reminder_confirmations("G1", reclaimed_claims) == 2
    assert memory.claim_reminder_confirmations("G1", limit=4) == []


def test_stale_outbox_owner_cannot_release_new_claim(temp_db):
    import time
    import memory

    memory.enqueue_reminder_confirmation("G1", "pending:1", "第一筆")
    first = memory.claim_reminder_confirmations("G1", limit=1)
    first_claim = [(first[0]["confirmation_id"], first[0]["claim_token"])]
    with memory._conn() as c:
        c.execute(
            "UPDATE reminder_confirmation_outbox SET claimed_at=? "
            "WHERE confirmation_id=?",
            (int(time.time()) - 1200, first[0]["confirmation_id"]),
        )
    second = memory.claim_reminder_confirmations("G1", limit=1)
    second_claim = [(second[0]["confirmation_id"], second[0]["claim_token"])]

    assert second_claim[0][1] != first_claim[0][1]
    assert memory.release_reminder_confirmations("G1", first_claim) == 0
    assert memory.delete_sent_reminder_confirmations("G1", first_claim) == 0
    assert memory.claim_reminder_confirmations("G1", limit=1) == []
    assert memory.release_reminder_confirmations("G1", second_claim) == 1


def test_drain_completion_rolls_back_the_reminder_when_closing_fails(temp_db):
    import memory

    pending_id = memory.enqueue_pending_reminder(
        "G1", "U1", "明天早上9點開會", "m1"
    )
    assert pending_id is not None
    pending_claim_token = memory.claim_pending_reminder(pending_id)
    assert pending_claim_token
    remind_at = int((datetime.now() + timedelta(days=1)).timestamp())
    with memory._conn() as c:
        c.execute(
            "CREATE TRIGGER fail_pending_done BEFORE UPDATE ON pending_reminder_extract "
            "WHEN NEW.status='done' BEGIN "
            "SELECT RAISE(ABORT, 'forced completion failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced completion failure"):
        memory.complete_pending_reminder(
            pending_id=pending_id,
            pending_claim_token=pending_claim_token,
            group_id="G1",
            user_id="U1",
            action="開會",
            remind_at=remind_at,
            source_text="明天早上9點開會",
            mention_aliases=[],
        )

    with memory._conn() as c:
        reminder_count = c.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
        outbox_count = c.execute(
            "SELECT COUNT(*) FROM reminder_confirmation_outbox"
        ).fetchone()[0]
        pending_status = c.execute(
            "SELECT status FROM pending_reminder_extract WHERE pending_id=?",
            (pending_id,),
        ).fetchone()[0]
    assert reminder_count == 0
    assert outbox_count == 0
    assert pending_status == "processing"
    memory.release_pending_reminder(pending_id, pending_claim_token)


def test_add_reminder_merges_mana_group_duplicate_with_details(temp_db):
    """Same-time Mana group reminders should merge richer details instead of duplicating."""
    import memory

    future = datetime.now() + timedelta(days=1)
    remind_at = int(future.timestamp())

    rid1 = memory.add_reminder(
        "G1",
        "U1",
        "媽媽行程：嗎哪小組（19:15-21:30）",
        remind_at,
        source_text="媽媽排程圖片：6/5 19:15-21:30 嗎哪小組",
    )
    rid2 = memory.add_reminder(
        "G1",
        "U1",
        "去教會4樓參加嗎？那小組的茶几",
        remind_at,
        source_text="明天晚上7:15我要去教會4樓參加嗎？那小組的茶几",
    )

    with memory._conn() as c:
        rows = c.execute(
            "SELECT reminder_id, action, source_text FROM reminders "
            "WHERE group_id='G1' ORDER BY reminder_id"
        ).fetchall()
    assert rid2 == rid1
    assert len(rows) == 1
    assert rows[0][1] == "媽媽行程：嗎哪小組查經（教會4樓，19:15-21:30）"
    assert "媽媽排程圖片" in rows[0][2]
    assert "教會4樓參加嗎哪小組的查經" in rows[0][2]
