from __future__ import annotations

from pathlib import Path


LINE_BOT_DIR = Path(__file__).resolve().parent
TOKEN_SOURCE_ALLOWLIST = {
    "config.py",
    "line_push_client.py",
    "line_token_refresh.py",
    "preflight_check.py",
    "preflight_cloud.py",
}


def _production_py_files() -> list[Path]:
    return [
        path
        for path in LINE_BOT_DIR.rglob("*.py")
        if not path.name.startswith("test_")
        and path.name != "conftest.py"
        and ".venv" not in path.parts
    ]


def test_push_scripts_do_not_read_legacy_line_token_directly():
    forbidden = (
        'os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"',
        "os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'",
        "settings.line_channel_access_token",
    )
    offenders: list[str] = []
    for path in _production_py_files():
        if path.name in TOKEN_SOURCE_ALLOWLIST:
            continue
        text = path.read_text(encoding="utf-8")
        if any(pattern in text for pattern in forbidden):
            offenders.append(path.name)

    assert offenders == []


def test_line_push_callers_use_shared_token_helper():
    offenders: list[str] = []
    for path in _production_py_files():
        text = path.read_text(encoding="utf-8")
        is_push_caller = (
            "/v2/bot/message/push" in text
            or "PushMessageRequest" in text
            or ".push_message(" in text
        )
        if not is_push_caller:
            continue
        if path.name == "line_push_client.py":
            continue
        if "line_push_client" not in text:
            offenders.append(path.name)

    assert offenders == []


def test_raw_line_push_url_stays_in_shared_client_only():
    offenders: list[str] = []
    for path in _production_py_files():
        if path.name == "line_push_client.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "https://api.line.me/v2/bot/message/push" in text:
            offenders.append(str(path.relative_to(LINE_BOT_DIR)))

    assert offenders == []
