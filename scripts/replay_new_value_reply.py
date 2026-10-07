"""Replay explicit @咪寶 text against real providers, with no LINE delivery.

Checks the 2026-09-26 "only corrections / suggestions / new info" policy end to
end: real Claude CLI / Gemini generation, the Gemini quality gate, and the
Gemini-lite restatement judge.  SQLite, fact cache and chat memory are isolated;
LINE transport is forbidden.  Fixtures are synthetic.

Usage (from line_bot/):
    .venv/bin/python scripts/replay_new_value_reply.py            # built-in cases
    echo "自訂訊息" | .venv/bin/python scripts/replay_new_value_reply.py -
"""
import contextlib
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parents[1]

CASES = [
    ("含錯誤的說法（應糾正）",
     "我查過了，淘寶要用大陸版就一定要有大陸手機號碼才能註冊，所以台灣人只能用台灣版。"),
    ("沒有錯的提醒（應補新資訊或不回）",
     "中秋烤肉生熟食的夾子一定要分開，不然很容易吃壞肚子，大家要小心。"),
    ("問句（應直接回答）", "HRV 是什麼？數字越高越好嗎？"),
    ("明確要求整理（摘要例外）",
     "幫我整理重點：乙平台是轉單給甲商店，所以多一層手續費，出貨也比較慢，直接在甲商店買才便宜。"),
]


def run_replay(cases):
    os.chdir(REPO)
    sys.path.insert(0, str(REPO))
    with tempfile.TemporaryDirectory(prefix="line-new-value-replay-") as temporary:
        os.environ["SQLITE_PATH"] = str(Path(temporary) / "isolated.sqlite3")
        os.environ["BOT_MUTED"] = "true"
        os.environ["LINE_BOT_RESTATEMENT_JUDGE"] = "1"
        import claude_client
        import fulltext_fetcher
        import gemini_client
        import main
        import notify_discord
        import restatement_judge
        from linebot.v3.webhooks import MessageEvent, TextMessageContent

        logging.disable(logging.CRITICAL)
        main._configure_local_text_llm_runtime()
        main._load_quota_state()
        quota_copy = Path(temporary) / "quota.json"
        if Path(main._QUOTA_STATE_FILE).exists():
            quota_copy.write_bytes(Path(main._QUOTA_STATE_FILE).read_bytes())
        main._QUOTA_STATE_FILE = str(quota_copy)

        results = []
        for label, message in cases:
            result = {"case": label, "message": message, "providers": [],
                      "draft": None, "judge_raw": None, "reply": "",
                      "completed_no_reply": False, "line_sent": False}

            def capture_reply(token, text, **kwargs):
                result["reply"] = main._prepare_outbound_text(text, source="isolated-replay")
                return False

            def deny_delivery(*args, **kwargs):
                raise AssertionError("LINE transport forbidden during replay")

            def provider(name, function):
                def wrapped(*args, **kwargs):
                    out = function(*args, **kwargs)
                    result["providers"].append({"provider": name, "produced_text": bool(out)})
                    return out
                return wrapped

            real_enforce = main._enforce_new_value_reply

            def observed_enforce(reply_text, **kwargs):
                result["draft"] = reply_text
                return real_enforce(reply_text, **kwargs)

            real_judge = restatement_judge._call_light_model

            def observed_judge(prompt):
                raw = real_judge(prompt)
                result["judge_raw"] = raw if raw is not None else "(judge call failed → fail-open)"
                return raw

            def mark_silent(*args, **kwargs):
                result["completed_no_reply"] = True
                return True

            evt = MagicMock(spec=MessageEvent)
            evt.message = TextMessageContent(id="M_REPLAY", text=message, quoteToken="qt")
            evt.source = SimpleNamespace(type="group", group_id="G_REPLAY", user_id="U_REPLAY")
            evt.reply_token = "T_REPLAY"
            with contextlib.ExitStack() as stack:
                for item in (
                    patch.object(main, "_reply", capture_reply),
                    patch.object(main, "ApiClient", deny_delivery),
                    patch.object(main, "_thinking_indicator", lambda *_: contextlib.nullcontext()),
                    patch.object(main, "_build_quoted_block", lambda *a, **k: ""),
                    patch.object(main, "_maybe_extract_facts", lambda *a, **k: None),
                    patch.object(main, "_maybe_capture_calendar_event", lambda *a, **k: None),
                    patch.object(main, "_try_save_correction", lambda *a, **k: None),
                    patch.object(main, "_get_persona_notes", lambda *_: []),
                    patch.object(main, "_mark_inbound_reply_completed_no_reply", mark_silent),
                    patch.object(main, "_enforce_new_value_reply", observed_enforce),
                    patch.object(main.memory, "get_context", lambda *_: []),
                    patch.object(main.memory, "top_facts", lambda *a, **k: []),
                    patch.object(main.memory, "append_turn", lambda *a: None),
                    patch.object(fulltext_fetcher, "DEFAULT_CACHE_DB", Path(temporary) / "web_cache.sqlite3"),
                    patch.object(restatement_judge, "_call_light_model", observed_judge),
                    patch.object(gemini_client, "_log_quality_violation", lambda *a: None),
                    patch.object(gemini_client, "_alert_quality_violation", lambda *a: None),
                    patch.object(notify_discord, "send_dm", lambda *a, **k: False),
                    patch.object(claude_client, "chat", provider("claude", claude_client.chat)),
                    patch.object(gemini_client, "chat", provider("gemini", gemini_client.chat)),
                ):
                    stack.enter_context(item)
                try:
                    main._handle_explicit_text(evt, "G_REPLAY", message)
                except Exception as exc:  # report, keep replaying other cases
                    result["error_type"] = type(exc).__name__
            results.append(result)
        print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if sys.argv[1:] == ["-"]:
        text = sys.stdin.readline().strip()
        if not text:
            raise SystemExit("missing message on stdin")
        run_replay([("自訂", text)])
    else:
        run_replay(CASES)
