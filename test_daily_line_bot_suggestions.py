import json
import sqlite3
import sys
import types
from datetime import datetime, timedelta

import daily_briefing_discord as dbd


def _make_raw_messages_db(
    path,
    now,
    texts,
    *,
    group_id="G1",
    old_texts=None,
    bot_texts=None,
    null_user_texts=None,
    other_group_texts=None,
):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE raw_messages (
            group_id    TEXT NOT NULL,
            message_id  TEXT NOT NULL,
            user_id     TEXT,
            text        TEXT NOT NULL,
            created_at  INTEGER NOT NULL,
            category    TEXT,
            PRIMARY KEY (group_id, message_id)
        )
        """
    )
    for idx, text in enumerate(texts):
        conn.execute(
            "INSERT INTO raw_messages VALUES (?,?,?,?,?,?)",
            (
                group_id,
                f"m{idx}",
                f"U{idx}",
                text,
                int((now - timedelta(hours=idx)).timestamp()),
                None,
            ),
        )
    for idx, text in enumerate(old_texts or []):
        conn.execute(
            "INSERT INTO raw_messages VALUES (?,?,?,?,?,?)",
            (
                group_id,
                f"old{idx}",
                f"U_old{idx}",
                text,
                int((now - timedelta(days=4, hours=idx)).timestamp()),
                None,
            ),
        )
    for idx, text in enumerate(bot_texts or []):
        conn.execute(
            "INSERT INTO raw_messages VALUES (?,?,?,?,?,?)",
            (
                group_id,
                f"bot{idx}",
                "__bot__",
                text,
                int((now - timedelta(hours=idx)).timestamp()),
                None,
            ),
        )
    for idx, text in enumerate(null_user_texts or []):
        conn.execute(
            "INSERT INTO raw_messages VALUES (?,?,?,?,?,?)",
            (
                group_id,
                f"null{idx}",
                None,
                text,
                int((now - timedelta(hours=idx)).timestamp()),
                None,
            ),
        )
    for idx, text in enumerate(other_group_texts or []):
        conn.execute(
            "INSERT INTO raw_messages VALUES (?,?,?,?,?,?)",
            (
                "G_OTHER",
                f"other{idx}",
                f"U_other{idx}",
                text,
                int((now - timedelta(hours=idx)).timestamp()),
                None,
            ),
        )
    conn.commit()
    conn.close()


def _install_fake_genai(monkeypatch, *, response_text="", side_effect=None):
    prompts = []

    class FakeResponse:
        text = response_text

    class FakeModels:
        def generate_content(self, *, model, contents, config):
            prompts.append(contents)
            if side_effect:
                raise side_effect
            return FakeResponse()

    class FakeClient:
        def __init__(self, api_key=None):
            self.models = FakeModels()

    google_module = types.ModuleType("google")
    genai_module = types.ModuleType("google.genai")
    types_module = types.ModuleType("google.genai.types")
    types_module.GenerateContentConfig = lambda **kwargs: kwargs
    genai_module.Client = FakeClient
    genai_module.types = types_module
    google_module.genai = genai_module
    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.genai", genai_module)
    monkeypatch.setitem(sys.modules, "google.genai.types", types_module)
    return prompts


def test_line_bot_daily_recommendation_uses_recent_group_signal(tmp_path, monkeypatch):
    now = datetime(2026, 6, 19, 10, 0)
    db_path = tmp_path / "line_bot.db"
    history_path = tmp_path / "suggestion_history.json"
    monkeypatch.setattr(dbd, "LINE_BOT_DIR", tmp_path)
    _make_raw_messages_db(
        db_path,
        now,
        [
            "爸爸明天要去醫院檢查，記得提醒吃藥",
            "最近睡眠和呼吸好像都不太穩，要不要看醫生",
        ],
    )

    msg = dbd.line_bot_suggestions(
        now=now,
        db_path=db_path,
        history_path=history_path,
        group_id="G1",
        use_ai=False,
    )

    assert "💡 **LINE bot 每日推薦**" in msg
    assert "健康事件追蹤與就醫提醒" in msg
    assert "近 3 天" in msg
    history = json.loads(history_path.read_text())
    assert history[-1]["title"] == "健康事件追蹤與就醫提醒"


def test_line_bot_daily_recommendation_defaults_to_local_only(tmp_path, monkeypatch):
    now = datetime(2026, 6, 19, 10, 0)
    db_path = tmp_path / "line_bot.db"
    history_path = tmp_path / "suggestion_history.json"
    monkeypatch.setattr(dbd, "LINE_BOT_DIR", tmp_path)
    monkeypatch.delenv("LINE_BOT_SUGGESTIONS_USE_AI", raising=False)
    _make_raw_messages_db(db_path, now, ["明天要提醒媽媽回診，還要記得帶檢查報告"])
    prompts = _install_fake_genai(
        monkeypatch,
        response_text=json.dumps(
            {"suggestion": {"title": "AI 行程提醒", "reason": "近期群聊有回診提醒需求。"}},
            ensure_ascii=False,
        ),
    )

    msg = dbd.line_bot_suggestions(
        now=now,
        db_path=db_path,
        history_path=history_path,
        group_id="G1",
    )

    assert "AI 行程提醒" not in msg
    assert prompts == []
    assert "LINE bot 每日推薦" in msg


def test_line_bot_daily_recommendation_uses_legacy_line_group_env(
    tmp_path, monkeypatch
):
    now = datetime(2026, 6, 19, 10, 0)
    db_path = tmp_path / "line_bot.db"
    history_path = tmp_path / "suggestion_history.json"
    monkeypatch.setattr(dbd, "LINE_BOT_DIR", tmp_path)
    monkeypatch.delenv("FAMILY_GROUP_ID", raising=False)
    monkeypatch.delenv("ALLOWED_GROUP_ID", raising=False)
    monkeypatch.setenv("LINE_ALLOWED_GROUP_ID", "G1")
    _make_raw_messages_db(
        db_path,
        now,
        ["爸爸明天要去醫院檢查，記得提醒吃藥"],
        other_group_texts=["今晚晚餐要煮什麼"],
    )

    msg = dbd.line_bot_suggestions(
        now=now,
        db_path=db_path,
        history_path=history_path,
        use_ai=False,
    )

    assert "健康事件追蹤與就醫提醒" in msg


def test_line_bot_daily_recommendation_includes_recent_messages_in_ai_prompt(
    tmp_path, monkeypatch
):
    now = datetime(2026, 6, 19, 10, 0)
    db_path = tmp_path / "line_bot.db"
    history_path = tmp_path / "suggestion_history.json"
    monkeypatch.setattr(dbd, "LINE_BOT_DIR", tmp_path)
    _make_raw_messages_db(
        db_path,
        now,
        ["明天要提醒媽媽回診，還要記得帶檢查報告"],
        old_texts=["四天前的舊訊息不該進 prompt"],
        bot_texts=["bot 自己的訊息不該進 prompt"],
        null_user_texts=["沒有 user id 的真人訊息也要進 prompt"],
        other_group_texts=["其他群組的私密訊息不該進 prompt"],
    )
    prompts = _install_fake_genai(
        monkeypatch,
        response_text=json.dumps(
            {"suggestion": {"title": "AI 行程提醒", "reason": "近期群聊有回診提醒需求。"}},
            ensure_ascii=False,
        ),
    )

    msg = dbd.line_bot_suggestions(
        now=now,
        db_path=db_path,
        history_path=history_path,
        group_id="G1",
        use_ai=True,
    )

    assert "AI 行程提醒" in msg
    assert prompts
    prompt = prompts[0]
    assert "明天要提醒媽媽回診" in prompt
    assert "沒有 user id 的真人訊息" in prompt
    assert "其他群組的私密訊息" not in prompt
    assert "四天前的舊訊息" not in prompt
    assert "bot 自己的訊息" not in prompt


def test_line_bot_daily_recommendation_ai_null_uses_local_fallback(
    tmp_path, monkeypatch
):
    now = datetime(2026, 6, 19, 10, 0)
    db_path = tmp_path / "line_bot.db"
    history_path = tmp_path / "suggestion_history.json"
    monkeypatch.setattr(dbd, "LINE_BOT_DIR", tmp_path)
    _make_raw_messages_db(db_path, now, ["今晚晚餐要煮什麼，冰箱有蛋和高麗菜"])
    _install_fake_genai(monkeypatch, response_text=json.dumps({"suggestion": None}))

    msg = dbd.line_bot_suggestions(
        now=now,
        db_path=db_path,
        history_path=history_path,
        group_id="G1",
        use_ai=True,
    )

    assert "今天沒有新建議" not in msg
    assert "家庭飲食偏好與食材媒合" in msg


def test_line_bot_daily_recommendation_ai_quota_uses_local_fallback(
    tmp_path, monkeypatch
):
    now = datetime(2026, 6, 19, 10, 0)
    db_path = tmp_path / "line_bot.db"
    history_path = tmp_path / "suggestion_history.json"
    monkeypatch.setattr(dbd, "LINE_BOT_DIR", tmp_path)
    _make_raw_messages_db(db_path, now, ["下週媽媽回診，明天要先提醒帶藥單"])
    _install_fake_genai(
        monkeypatch,
        response_text="",
        side_effect=RuntimeError("429 RESOURCE_EXHAUSTED"),
    )

    msg = dbd.line_bot_suggestions(
        now=now,
        db_path=db_path,
        history_path=history_path,
        group_id="G1",
        use_ai=True,
    )

    assert "今天沒有新建議" not in msg
    assert "行程與提醒自動建議" in msg


def test_line_bot_daily_recommendation_never_disappears_without_db(tmp_path, monkeypatch):
    now = datetime(2026, 6, 19, 10, 0)
    monkeypatch.setattr(dbd, "LINE_BOT_DIR", tmp_path)

    msg = dbd.line_bot_suggestions(
        now=now,
        db_path=tmp_path / "missing.db",
        history_path=tmp_path / "suggestion_history.json",
        group_id="G1",
        use_ai=False,
    )

    assert "💡 **LINE bot 每日推薦**" in msg
    assert "今天沒有新建議" not in msg
    assert "群聊需求雷達" in msg


def test_line_bot_daily_recommendation_skips_history_title(tmp_path, monkeypatch):
    now = datetime(2026, 6, 19, 10, 0)
    db_path = tmp_path / "line_bot.db"
    history_path = tmp_path / "suggestion_history.json"
    monkeypatch.setattr(dbd, "LINE_BOT_DIR", tmp_path)
    history_path.write_text(
        json.dumps(
            [{"date": "2026-06-18T10:00:00", "title": "健康事件追蹤與就醫提醒"}],
            ensure_ascii=False,
        )
    )
    _make_raw_messages_db(
        db_path,
        now,
        [
            "爸爸明天要去醫院檢查，記得提醒吃藥",
            "下週回診前幾點出門要再確認",
        ],
    )

    msg = dbd.line_bot_suggestions(
        now=now,
        db_path=db_path,
        history_path=history_path,
        group_id="G1",
        use_ai=False,
    )

    assert "健康事件追蹤與就醫提醒" not in msg
    assert "行程與提醒自動建議" in msg


def test_line_bot_daily_recommendation_does_not_overwrite_corrupt_history(
    tmp_path, monkeypatch
):
    now = datetime(2026, 6, 19, 10, 0)
    db_path = tmp_path / "line_bot.db"
    history_path = tmp_path / "suggestion_history.json"
    monkeypatch.setattr(dbd, "LINE_BOT_DIR", tmp_path)
    history_path.write_text("{not valid json")
    _make_raw_messages_db(db_path, now, ["今晚晚餐要煮什麼"])

    msg = dbd.line_bot_suggestions(
        now=now,
        db_path=db_path,
        history_path=history_path,
        group_id="G1",
        use_ai=False,
    )

    assert "LINE bot 每日推薦" in msg
    assert history_path.read_text() == "{not valid json"
