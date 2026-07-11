from __future__ import annotations

import json

import claude_client as cc


def _configure(monkeypatch, tmp_path):
    monkeypatch.setattr(cc.settings, "claude_api_key", "test-key")
    monkeypatch.setattr(cc.settings, "claude_model", "claude-test")
    monkeypatch.setattr(cc.settings, "claude_quota_cooldown_sec", 300)
    monkeypatch.setattr(cc.settings, "claude_use_cli", False)
    monkeypatch.setattr(cc, "_STATE_FILE", tmp_path / "claude-state.json")


def test_no_key_keeps_gemini_path(monkeypatch):
    monkeypatch.setattr(cc.settings, "claude_api_key", "")
    called = []
    monkeypatch.setattr(cc, "_request", lambda payload: called.append(payload))

    assert cc.chat("你好", [], []) is None
    assert called == []


def test_successful_claude_reply_does_not_fallback(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(cc, "_build_payload", lambda *args: {"messages": []})
    calls = []
    monkeypatch.setattr(
        cc,
        "_request",
        lambda payload: calls.append(payload) or {
            "content": [{"type": "text", "text": "Claude 回覆"}],
        },
    )

    assert cc.chat("問題", [], []) == "Claude 回覆"
    assert len(calls) == 1
    assert cc.quota_exhausted() is False


def test_quota_failure_persists_gate_and_skips_next_call(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(cc, "_build_payload", lambda *args: {})
    calls = {"count": 0}

    def fail(_payload):
        calls["count"] += 1
        raise cc.ClaudeQuotaExhausted("credit balance exhausted")

    monkeypatch.setattr(cc, "_request", fail)

    assert cc.chat("問題", [], []) is None
    assert cc.quota_exhausted()
    assert calls["count"] == 1
    saved = json.loads((tmp_path / "claude-state.json").read_text())
    assert saved["quota_exhausted_until"] > 0

    assert cc.chat("第二題", [], []) is None
    assert calls["count"] == 1


def test_ordinary_failure_does_not_open_quota_gate(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(cc, "_build_payload", lambda *args: {})
    monkeypatch.setattr(
        cc,
        "_request",
        lambda _payload: (_ for _ in ()).throw(cc.ClaudeProviderError("timeout")),
    )

    assert cc.chat("問題", [], []) is None
    assert cc.quota_exhausted() is False
    assert not (tmp_path / "claude-state.json").exists()


def test_api_credit_error_switches_to_logged_in_cli(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(cc, "_build_payload", lambda *args: {})
    monkeypatch.setattr(
        cc,
        "_request",
        lambda _payload: (_ for _ in ()).throw(
            cc.ClaudeQuotaExhausted(
                "HTTP 400: Your credit balance is too low to access the Anthropic API"
            )
        ),
    )
    monkeypatch.setattr(cc, "_chat_via_cli", lambda *args: "CLI 回覆")

    assert cc.chat("問題", [], []) == "CLI 回覆"
    saved = json.loads((tmp_path / "claude-state.json").read_text())
    assert saved["prefer_cli"] is True
    assert "quota_exhausted_until" not in saved


def test_preferred_cli_skips_api(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    (tmp_path / "claude-state.json").write_text(json.dumps({"prefer_cli": True}))
    monkeypatch.setattr(cc, "_request", lambda _payload: (_ for _ in ()).throw(
        AssertionError("API should not be called after CLI preference is saved")
    ))
    monkeypatch.setattr(cc, "_chat_via_cli", lambda *args: "CLI 回覆")

    assert cc.chat("問題", [], []) == "CLI 回覆"


def test_main_route_prefers_claude(monkeypatch):
    import main

    monkeypatch.setattr(
        "claude_client.chat", lambda *args, **kwargs: "Claude 優先答案"
    )
    gemini = []
    monkeypatch.setattr(
        main,
        "_gemini_llm_chat",
        lambda *args, **kwargs: gemini.append(True) or "Gemini 答案",
    )

    assert main._llm_chat("問題", [], []) == "Claude 優先答案"
    assert gemini == []


def test_main_route_falls_back_to_existing_gemini_chain(monkeypatch):
    import main

    monkeypatch.setattr("claude_client.chat", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_gemini_llm_chat", lambda *args, **kwargs: "Gemini 答案")

    assert main._llm_chat("問題", [], []) == "Gemini 答案"
