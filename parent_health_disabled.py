"""爸媽健康暫停使用（緊急停用版：在重啟鎖內把這個檔案換成 parent_health.py，見 ROLLBACK.md）。

main.py 的 /健康 分支照常把訊息接走，這裡一律回固定句：舊卡片上的按鈕仍然點得到，但不會掉進
一般路由交給模型。不抽取、不回 119、不私訊、不推群組；週報段落、週分析與異常檢查空白；背景什麼都不做。收回訊息
照樣刪掉那則留言的健康紀錄（盡力而為）。介面、常數與正式版相同（測試確認），不 import 正式版。
2026-10-23：沒有卡片所以沒有大字版網頁；運動提醒一律不提醒（不搶、不放去重列）。換上這個版本重啟時
（start_background）把健康卡的大字版網頁全部清掉，收回訊息刪到紀錄時也清（盡力而為）。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "FATHER",
    "MOTHER",
    "PARENTS",
    "FAMILY",
    "REPLY_PREFIX",
    "CARD_PREFIX",
    "BOT_PREFIXES",
    "SAFETY_TEXT",
    "FAILURE_TEXT",
    "QUOTE_NOTICE",
    "WEEKLY_MARKER",
    "SHIPPED_HEALTH_BUTTON_TEXTS",
    "CommandReply",
    "RedFlagDecision",
    "ExerciseNudge",
    "parent_user_id",
    "role_of",
    "handle_command",
    "red_flag_decision",
    "red_flag_outcome",
    "is_health_card_text",
    "observe_async",
    "observe_message",
    "forget_message",
    "weekly_section",
    "weekly_analysis_text",
    "anomaly_lines",
    "run_daily_check",
    "start_background",
    "stop_background",
    "status_summary",
    "exercise_nudge_enabled",
    "exercise_nudge_candidate",
    "claim_exercise_nudge",
    "release_exercise_nudge",
]

FATHER = "爸爸"
MOTHER = "媽媽"
PARENTS = (FATHER, MOTHER)
FAMILY = "family"

REPLY_PREFIX = "爸媽健康："
CARD_PREFIX = "爸媽健康卡："
BOT_PREFIXES = (REPLY_PREFIX, CARD_PREFIX)
SAFETY_TEXT = (
    "爸媽健康：⚠️ 如果現在有胸痛胸悶、呼吸困難、嘴歪或說話不清、單側手腳無力，"
    "或跌倒後不舒服，請馬上就醫或打 119。"
)
FAILURE_TEXT = "爸媽健康：暫時無法回覆，請稍後再試"
QUOTE_NOTICE = "爸媽健康：卡片上是從群組留言整理的紀錄，分析是依公開衛教的一般建議，不是診斷；身體有疑問請問醫師。"
WEEKLY_MARKER = "🩺 爸媽這週"
SHIPPED_HEALTH_BUTTON_TEXTS: tuple[str, ...] = (
    "/健康",
    "/健康 爸爸",
    "/健康 媽媽",
    "/健康 同意",
    "/健康 恢復",
    "/健康 吃藥了",
    "/健康 有運動",
    "/健康 說明",
    "/健康 分析",
)

_BACKGROUND_ENABLED = False
_DISABLED_TEXT = "爸媽健康：暫停使用中"  # 不寫「恢復後會再通知」：承諾掃描會把它當成咪寶答應的事


@dataclass(frozen=True)
class CommandReply:
    text: str | None = None
    card: dict | None = None
    alt_text: str | None = None
    fallback_text: str = FAILURE_TEXT
    page_card: dict | None = None


@dataclass(frozen=True)
class RedFlagDecision:
    role: str
    labels: tuple[str, ...]
    reply: bool
    allow_push: bool
    dm: bool
    reason: str
    event_ts: float = 0.0
    others: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class ExerciseNudge:
    role: str
    user_id: str
    text: str
    label: str
    archive_text: str = "爸媽健康：運動提醒"


def _purge_health_pages() -> None:
    """健康卡的大字版網頁全部清掉（盡力而為；card_pages 用到時才 import）。"""
    try:
        import card_pages

        card_pages.purge_health_pages()
    except Exception:
        pass


def parent_user_id(role: str) -> str | None:
    return None


def role_of(user_id: str | None) -> str:
    return FAMILY


def handle_command(
    group_id: str,
    user_id: str | None,
    body: str,
    *,
    message_id: str,
    quoted_message_id: str | None = None,
    now: float | None = None,
) -> CommandReply:
    """停用期間：引用留言＋「/健康 刪除」照樣刪（使用者要求的刪除不能停）；其他一律回固定句。"""
    command = " ".join(str(body or "").split())
    if command == "刪除" and quoted_message_id:
        deleted = forget_message(group_id, quoted_message_id)
        if deleted:
            return CommandReply(text=f"爸媽健康：已刪掉那則留言記下的 {deleted} 筆紀錄")
        return CommandReply(text="爸媽健康：那則留言沒有記下健康紀錄")
    if command == "刪除我的紀錄":
        return CommandReply(text="爸媽健康：暫停使用中；要刪掉全部紀錄，請告訴 Andrew")
    return CommandReply(text=_DISABLED_TEXT)


def red_flag_decision(
    group_id: str,
    user_id: str | None,
    text: str,
    *,
    message_id: str,
    event_ts_ms,
    now: float | None = None,
) -> RedFlagDecision | None:
    return None


def red_flag_outcome(
    group_id: str,
    decision: RedFlagDecision | None,
    *,
    message_id: str,
    sent: bool,
    suppressed: bool,
    muted: bool,
    now: float | None = None,
) -> None:
    return None


def is_health_card_text(text: str) -> bool:
    return isinstance(text, str) and text.lstrip().startswith(CARD_PREFIX)


def observe_async(group_id: str, message_id: str, user_id: str | None, text: str, *, event_ts_ms) -> None:
    return None


def observe_message(
    group_id: str,
    message_id: str,
    user_id: str | None,
    text: str,
    *,
    event_ts_ms,
    db_path=None,
    now: float | None = None,
) -> int:
    return 0


def forget_message(group_id: str, message_id: str, *, db_path=None) -> int:
    """停用期間收回訊息照樣刪掉那則留言的健康紀錄（使用者要求的刪除不能停）；刪到紀錄時大字版網頁也清掉。"""
    try:
        import parent_health_db

        deleted = parent_health_db.delete_message_events(group_id, str(message_id or ""), db_path=db_path)
    except Exception:
        return 0
    if deleted:
        _purge_health_pages()
    return deleted


def weekly_section(group_id: str, now: float | None = None) -> str:
    return ""


def weekly_analysis_text(group_id: str, now: float | None = None) -> str | None:
    return None


def anomaly_lines(group_id: str, now: float | None = None) -> list[str]:
    return []


def run_daily_check(now: float | None = None) -> int:
    return 0


def start_background() -> None:
    """換上停用版重啟時：健康卡的大字版網頁全部清掉（緊急停用不能留著拿到連結就看得到的頁）。"""
    _purge_health_pages()
    return None


def stop_background(timeout: float = 2.0) -> None:
    return None


def status_summary(now: float | None = None) -> dict:
    return {"status": "disabled"}


def exercise_nudge_enabled() -> bool:
    return False


def exercise_nudge_candidate(
    group_id: str, messages: list[tuple[str, str | None]], *, now: float | None = None
) -> ExerciseNudge | None:
    return None


def claim_exercise_nudge(
    group_id: str, nudge: ExerciseNudge | None, *, now: float | None = None
) -> tuple[str, int] | None:
    return None


def release_exercise_nudge(group_id: str, nudge: ExerciseNudge | None, claim: tuple[str, int] | None) -> None:
    return None
