"""爸媽健康（/健康，2026-10-11）：從群組留言整理爸媽的用藥、看診、運動、睡眠、飲食、身體狀況。

Andrew 2026-10-11 選全 A；2026-10-17 改成「健康預設同意紀錄，不需要再有我同意紀錄的設計」，
同一天又說「不要任何本人按鍵的功能（ex:我吃藥了，我有運動），因為他們不會去按」：
家族主群（FAMILY_GROUP_ID）預設就記錄爸媽（第一次看到時自動寫 on 同意列，changed_by='default'），
其他群組照舊要本人打 /健康 恢復。誰都能叫出卡片、誰都能幫忙暫停；家人暫停的家人也能恢復
（/健康 恢復 爸爸），爸媽自己暫停的只有本人能恢復。卡片上沒有要爸媽本人按的按鈕；
來源是爸媽自己說的、兄弟姊妹提到爸媽的（標明誰說的），舊卡片上的「我吃藥了／我有運動」本人按了照樣記；
健康卡＋週日週報一段＋異常私訊 Andrew；記錄中的家長提到胸痛、跌倒這類緊急狀況，群組回就醫／119
提醒並私訊 Andrew。
2026-10-17 加「/健康 分析」（Andrew 選全 A）：規則與句子在 parent_health_analysis（純函式，用到時才
import）；分析卡給全家看（不寫 119），週日晚上私訊 Andrew 完整版，很高的血壓、低血糖併進每日檢查。

- 全部規則式（parent_health_extract、parent_health_analysis），不送模型或外部服務。Discord 私訊帶角色、
  類別、標籤、天數／時間；很高的血壓、低血糖與週日分析另帶讀數（Andrew 看過含數字的範例後選全 A）。
- 卡片與文字只由固定詞表、數字、日期組成，沒有留言原文；卡片 altText 固定、不含資料（存檔只留 altText）。
- 角色只認本機別名檔與角色檔（不用 LINE 顯示名稱）；同意列只在「現在解析到的帳號＝列上的帳號」時有效
  （家族主群的預設列在帳號換人時自動改成新帳號，on_since 照舊）。
- 不變式：on 列的 changed_by＝user_id ⇔ 爸媽本人自己同意（預設列是 'default'、家人恢復的是家人帳號）；
  ROLLBACK §4 要改回本人同意時，靠這條選出非本人同意的列。
- main／weekly_summary 只 import 這個模組；公開函式都不丟例外（出錯回安全值，log 只記 error_type）。
- import 時不建表、不起執行緒或 executor、不讀檔；背景工作第一次用到才建，conftest 會把
  _BACKGROUND_ENABLED 關掉。
- 緊急停用：把 parent_health_disabled.py 換上來（同介面）。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import parent_health_db

logger = logging.getLogger("parent_health")

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
    "anomaly_lines",
    "run_daily_check",
    "start_background",
    "stop_background",
    "status_summary",
]

# 和 parent_health_extract 相同（測試確認）；這裡寫死，詞表模組壞掉時卡片、同意、暫停照樣能用。
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

# 卡片按鈕送出的文字（只加不刪：舊卡片永遠點得到）。吃藥了、有運動、恢復、同意 10/17 起新卡片不再出現。
SHIPPED_HEALTH_BUTTON_TEXTS: tuple[str, ...] = (
    "/健康",
    "/健康 爸爸",
    "/健康 媽媽",
    "/健康 同意",  # 舊卡片上的按鈕（2026-10-17 前）；現在等於 /健康 恢復
    "/健康 恢復",
    "/健康 吃藥了",
    "/健康 有運動",
    "/健康 說明",
    "/健康 分析",  # 2026-10-17 分析卡
)

_BACKGROUND_ENABLED = True  # conftest 會關掉（測試不能起真的背景工作）

_TPE = ZoneInfo("Asia/Taipei")
_WEEKDAYS = "一二三四五六日"
_ROLE_LOG = {FATHER: "father", MOTHER: "mother"}
_PRONOUN = {FATHER: "他", MOTHER: "她"}

# 卡片 altText：固定、不含資料（聊天列表、通知、raw_messages 存檔只看得到這行）。
_OVERVIEW_ALT = "爸媽健康卡：點開看爸媽近 7 天的整理"
_DETAIL_ALT = "爸媽健康卡：點開看近 14 天的細節"
_ANALYSIS_ALT = "爸媽健康卡：點開看爸媽近 14 天的分析"
_EMPTY = "沒有紀錄"  # 「-」「（無）」「（空）」會被 outbound validator 當成空白佔位擋掉

_RECORDING_EXPLANATION = (
    "咪寶會從群組留言整理爸爸、媽媽的用藥、看診、運動、睡眠、飲食和身體狀況（含血壓血糖讀數、不舒服），"
    "在「爸媽健康」卡片給全家看、週日週報有一段、異常和每週日的分析整理會私訊 Andrew；留言提到胸痛、跌倒這類緊急狀況，"
    "咪寶會在群組提醒就醫或打 119。暫停只停止健康紀錄、卡片、週報段落、私訊和 119 提醒；"
    "已記的紀錄會保留（記錯的那則，誰都可以引用它再打 /健康 刪除；本人可以打 /健康 刪除我的紀錄 全部刪掉；已經私訊出去的不會收回）；"
    "一般聊天功能照舊（留言還是會像平常一樣被咪寶讀到）。"
)
_HELP_TEXT = (
    "爸媽健康：怎麼用\n"
    "不用按按鈕：爸爸、媽媽在群組說吃藥了、去散步了、血壓 135/85，咪寶會自動記下\n"
    "看爸媽近 7 天的整理卡：打 /健康\n"
    "看近 14 天的細節：打 /健康 爸爸 或 /健康 媽媽\n"
    "暫停記錄：打 /健康 暫停 爸爸 或 /健康 暫停 媽媽（爸媽本人打 /健康 暫停 也可以）\n"
    "恢復記錄：打 /健康 恢復 爸爸 或 /健康 恢復 媽媽（爸媽自己暫停的，要本人打 /健康 恢復）\n"
    "看分析和建議：打 /健康 分析\n"
    "記錯了：長按那則留言→回覆→打 /健康 刪除\n"
    "刪掉自己的全部紀錄：本人打 /健康 刪除我的紀錄\n"
    f"{_RECORDING_EXPLANATION}\n"
    "分析是依國健署等公開衛教整理的一般建議，不是診斷。"
)
_CARD_NOTE = "從群組留言整理，只記錄、不診斷"
# 舊卡片上的「我吃藥了／我有運動」（10/17 起新卡片不再有）
_AUTO_RECORD_TEXT = "爸媽健康：不用按了，爸爸、媽媽在群組說吃藥了、去散步了，或家人說爸爸吃藥了，咪寶就會自動記下"
_RECORDED_TEXT = "爸媽健康：記下了。以後在群組說吃藥了、去散步了也會自動記，不用按"
_ANALYSIS_NOTE = "依國健署等公開衛教整理，不是診斷；不舒服請看醫師"
_ANALYSIS_DM_NOTE = "每週日的完整整理會私訊 Andrew"

# 紀錄標籤（parent_health_extract.ALLOWED_ITEMS 裡的固定詞；測試確認都在詞表裡）
_MED_TAKEN = "吃藥"
_MED_FORGOT = "忘了吃藥"
_MED_PICKUP = "拿藥"
_MED_STOP = "停藥"
_SLEEP_BAD = "睡不好"
_SLEEP_GOOD = "睡得好"
_EXERCISE_BUTTON = "運動"
_BUTTON_RECORDS = {"吃藥了": ("med", _MED_TAKEN), "有運動": ("exercise", _EXERCISE_BUTTON)}

# 同意狀態
_ON, _PAUSED, _NONE, _RECONSENT, _UNSET = "on", "paused", "none", "reconsent", "unset"

_REPLY_WINDOW_S = 1800  # 同一家長 30 分鐘內只回一次 119 提醒
_DM_WINDOW_S = 1800  # 同一家長同一標籤 30 分鐘內只私訊一次
_EVENT_MAX_AGE_S = 600  # 超過 10 分鐘的訊息不回群組（只私訊）
_MAX_PUSH_REPLIES_PER_DAY = 2  # 今天成功送出的 119 回覆滿 2 則後不再 push fallback
_ALERT_SUPPRESS_S = 7 * 86400  # 每日檢查：同一項 7 天內只私訊一次
_URGENT_LOOKBACK_S = 2 * 86400  # 每日檢查看 48 小時內的危象血壓、低血糖（檢查時間會飄）
_URGENT_KINDS = ("bp_crisis", "glucose_low")  # 每筆讀數一個去重種類：同一筆只提醒一次，每種每天最多一行
_DAILY_HOUR = 21  # 台北 21:00 之後才做每日檢查
_CHECK_INTERVAL_S = 600.0
_STARTUP_DELAY_S = 60.0
_DM_MAX_CHARS = 1900
_CARD_MAX_BYTES = 30 * 1024
_LABEL_MAX = 20
_ACTION_TEXT_MAX = 300
_ALT_MAX = 1500


@dataclass(frozen=True)
class CommandReply:
    text: str | None = None
    card: dict | None = None
    alt_text: str | None = None
    fallback_text: str = FAILURE_TEXT


@dataclass(frozen=True)
class RedFlagDecision:
    role: str
    labels: tuple[str, ...]
    reply: bool  # 要不要在群組回 SAFETY_TEXT
    allow_push: bool  # 今天成功送出的 119 回覆 < 2
    dm: bool  # 要不要私訊
    reason: str  # "send" | "old_event" | "deduped" | "in_care" | "future"
    event_ts: float = 0.0  # 訊息時間（unix 秒；私訊寫幾點說的）
    # 同一則提到的另一位家長 (角色, 標籤, 那位的 reason)：只進私訊，結果照那位自己的寫
    others: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class _Record:
    """舊卡片的一鍵記錄用（和 parent_health_extract.Finding 欄位相同）。"""

    role: str
    category: str
    item: str
    value: str = ""
    flag: str = ""


# ── 小工具 ──────────────────────────────────────────────────────────────────


def _now(now: float | None) -> float:
    return time.time() if now is None else float(now)


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, _TPE)


def _today(now: float) -> date:
    return _local(now).date()


def _day_start_ts(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=_TPE).timestamp())


def _md(day: date) -> str:
    return f"{day.month}/{day.day}"


def _md_wd(day: date) -> str:
    return f"{day.month}/{day.day}（{_WEEKDAYS[day.weekday()]}）"


def _hhmm(ts: float) -> str:
    return _local(ts).strftime("%H:%M")


def _event_ts(event_ts_ms, now: float) -> float:
    """LINE 簽章過的 event.timestamp（毫秒）；沒有或壞掉時用現在。"""
    try:
        if event_ts_ms is not None:
            value = float(event_ts_ms) / 1000.0
            if value > 0:
                return value
    except (TypeError, ValueError):
        pass
    return now


def _err(where: str, exc: BaseException) -> None:
    logger.error("parent health %s failed error_type=%s", where, type(exc).__name__)


def _extract():
    """parent_health_extract（用到時才 import；測試可以換掉它的函式）。"""
    import parent_health_extract

    return parent_health_extract


def _clean_alias(name) -> str:
    return str(name or "").strip().lstrip("@").strip()


def _normalize(text: str | None) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


# ── 角色與同意 ──────────────────────────────────────────────────────────────

_role_issues_logged: set[tuple[str, str]] = set()


def _log_role_issue(role: str, issue: str) -> None:
    key = (role, issue)
    if key not in _role_issues_logged:  # 每個程序每種狀況只記一次（不含 ID）
        _role_issues_logged.add(key)
        logger.warning("parent health role %s role=%s", issue, _ROLE_LOG.get(role, "unknown"))


def _resolve_roles() -> dict[str, str | None]:
    """{爸爸: user_id 或 None, 媽媽: …}。

    只認：別名檔裡別名「恰好」是爸爸／媽媽的帳號，或角色檔明確把爸爸／媽媽對到某個別名、而那個
    別名只對到一個帳號。不走三字短名規則、不用顯示名稱。對到 0 個或多個帳號、或兩個角色對到同一個
    帳號 → 當成沒設定。
    """
    import line_mentions

    aliases = {
        uid: _clean_alias(label)
        for uid, label in line_mentions.load_user_aliases().items()
        if isinstance(uid, str) and uid
    }
    try:
        mapping = line_mentions.configured_family_alias_mapping(include_short=False)
    except Exception:
        mapping = {}
    resolved: dict[str, str | None] = {}
    for role in PARENTS:
        candidates = {uid for uid, label in aliases.items() if label == role}
        target = _clean_alias(mapping.get(role))
        if target and target != role:
            candidates |= {uid for uid, label in aliases.items() if label == target}
        if len(candidates) == 1:
            resolved[role] = next(iter(candidates))
        else:
            resolved[role] = None
            if candidates:
                _log_role_issue(role, "ambiguous")
    if resolved[FATHER] is not None and resolved[FATHER] == resolved[MOTHER]:
        _log_role_issue(FATHER, "shared_account")
        resolved = {FATHER: None, MOTHER: None}
    return resolved


_TITLE_WORDS = frozenset(
    {"爸爸", "媽媽", "爸", "媽", "老爸", "老媽", "父親", "母親", "爸比", "媽咪", "把拔", "馬麻", "爸媽", "爸爸媽媽"}
)


def _name_table(roles: dict[str, str | None]) -> dict[str, str | None]:
    """家人的名字型別名（含三字短名、角色檔的鍵）→ 爸媽的角色；其他家人 → None。

    家人用名字講爸媽（「大明剛剛跌倒了」）時先換成稱謂再抽取，不會記到說話的人身上，也不會因為
    「算別人」就漏掉 119（GP1 post #6、GP2 R2 #2）。只收兩個字以上；「爸爸」「媽媽」這類稱謂本身不收
    （別名本身就是稱謂時沒有東西要換）。其他家人的名字一起列進來，替換時長的先比，「小大明」不會被
    換成「小爸爸」（GP2 R3 #2）。"""
    import line_mentions

    aliases = line_mentions.load_user_aliases()
    parent_label = {role: _clean_alias(aliases.get(uid)) for role, uid in roles.items() if uid}
    label_role = {label: role for role, label in parent_label.items() if label}
    try:
        mapping = line_mentions.configured_family_alias_mapping(include_short=True)
    except Exception:
        mapping = {}
    table: dict[str, str | None] = {}
    for name, label in mapping.items():
        name = _clean_alias(name)
        if len(name) < 2 or name in _TITLE_WORDS:
            continue
        table[name] = label_role.get(_clean_alias(label))  # 角色檔的鍵（「爸鼻」）指到爸爸的標籤 → 爸爸
    for label, role in label_role.items():
        if len(label) >= 2 and label not in _TITLE_WORDS:
            table[label] = role
    return table


def _with_parent_titles(text: str, table: dict[str, str | None]) -> str:
    """一次掃過：爸媽的名字換成「爸爸」「媽媽」，其他家人的名字原樣保留（長的先比）。"""
    names = sorted(table, key=len, reverse=True)
    if not names or not any(table[name] for name in names):
        return text
    pattern = re.compile("|".join(map(re.escape, names)))
    return pattern.sub(lambda m: table[m.group(0)] or m.group(0), text)


def _other_names(table: dict[str, str | None]) -> tuple[str, ...]:
    """給抽取當「別人」的家人名字：名字表裡不是爸媽的那些。"""
    return tuple(sorted(name for name, role in table.items() if role is None))


def _is_command_text(text: str) -> bool:
    """「/」開頭的指令。先點名咪寶再下指令（「咪寶 /記住 …」）由 main 判斷、根本不會送進來：
    這裡不能比 main 的守門寬，不然 main 交給這裡的緊急字詞會被默默略過（GP2 R3 #1）。"""
    return _normalize(text).strip().startswith("/")


def _role_from(roles: dict[str, str | None], user_id: str | None) -> str:
    if user_id:
        for role in PARENTS:
            if roles.get(role) == user_id:
                return role
    return FAMILY


def parent_user_id(role: str) -> str | None:
    """爸爸／媽媽目前對到的 LINE 帳號（嚴格解析，見 _resolve_roles）；沒有或不明確回 None。"""
    try:
        return _resolve_roles().get(role)
    except Exception as exc:
        _err("role lookup", exc)
        return None


def role_of(user_id: str | None) -> str:
    """FATHER／MOTHER／FAMILY。"""
    try:
        return _role_from(_resolve_roles(), user_id)
    except Exception as exc:
        _err("role lookup", exc)
        return FAMILY


def _default_on(group_id: str) -> bool:
    """預設同意紀錄只用在家族主群（FAMILY_GROUP_ID；沒明示時 config 會拿舊的 ALLOWED_GROUP_ID 補上），
    兩個都沒設定就不預設（Andrew 2026-10-17）。"""
    try:
        family = str(getattr(parent_health_db.settings, "family_group_id", "") or "")
    except Exception:
        return False
    return bool(family) and group_id == family


def _ensure_default(group_id: str, roles: dict, consents: dict, *, now: float, db_path=None) -> bool:
    """家族主群裡、帳號已設定、還沒有同意列（或 on 但帳號換人）的家長補上預設的 on 列；有寫才回 True。
    先讀再寫：平常每則留言只讀、不拿寫入鎖；暫停的列不碰。失敗只記 error_type，卡片與指令照常。
    on_since 往前抓 _EVENT_MAX_AGE_S：比第一次寫列早一點、晚一點才處理的留言（webhook 先後、上線重啟後的
    重送）照樣記錄、照樣判 119，不會被 on_since 擋掉；分母和 17 天門檻只差這 10 分鐘。"""
    if not _default_on(group_id):
        return False
    accounts: dict[str, str] = {}
    for role in PARENTS:
        uid = roles.get(role)
        row = consents.get(role)
        if not uid:
            continue
        if row is None or (row.get("status") == "on" and row.get("user_id") != uid):
            accounts[role] = uid
    if not accounts:
        return False
    try:
        return parent_health_db.ensure_default_consent(
            group_id, accounts, now=now, on_since=now - _EVENT_MAX_AGE_S, db_path=db_path
        ) > 0
    except Exception as exc:
        _err("default consent", exc)
        return False


def _states(roles: dict[str, str | None], consents: dict[str, dict]) -> dict[str, str]:
    """每位家長：on（有效同意）／paused／none（沒同意過）／reconsent（同意時的帳號已不是現在
    解析到的帳號）／unset（帳號沒設定）。"""
    states: dict[str, str] = {}
    for role in PARENTS:
        uid = roles.get(role)
        row = consents.get(role)
        if uid is None:
            states[role] = _UNSET
        elif row is None:
            states[role] = _NONE
        elif row.get("status") == "paused":
            states[role] = _PAUSED
        elif row.get("status") == "on" and row.get("user_id") == uid:
            states[role] = _ON
        else:
            states[role] = _RECONSENT
    return states


@dataclass
class _Ctx:
    group_id: str
    now: float
    today: date
    roles: dict
    consents: dict
    states: dict
    db_path: object = None


def _context(group_id: str, now: float, db_path=None, *, ensure_default: bool = False) -> _Ctx:
    """ensure_default 只給指令用（webhook 本來就會寫）；週報、每日檢查、status_summary 不寫。"""
    roles = _resolve_roles()
    consents = parent_health_db.consent_states(group_id, db_path=db_path)
    if ensure_default and _ensure_default(group_id, roles, consents, now=now, db_path=db_path):
        consents = parent_health_db.consent_states(group_id, db_path=db_path)
    return _Ctx(group_id, now, _today(now), roles, consents, _states(roles, consents), db_path)


_SELF, _PARENT_ONLY, _BY_FAMILY = "self", "parent", "family"


def _known_member(user_id: str) -> bool:
    """本機別名檔裡有的帳號（家人，含爸媽）；讀不到就當成不認得。"""
    try:
        import line_mentions

        return bool(user_id) and bool(line_mentions.alias_for_user_id(user_id))
    except Exception:
        return False


def _resume_rule(ctx: _Ctx, role: str) -> str:
    """暫停中的 role 誰能恢復（Andrew 10/17「不要任何本人按鍵的功能」：家人暫停的不能只靠爸媽恢復）：
    - "self"：爸媽自己暫停的（changed_by＝列上的帳號或現在解析到的帳號），只有本人能恢復；
    - "family"：家族主群裡，認得出的家人（別名檔裡有的帳號，含另一位家長）或沒帶帳號的事件暫停的，
      認得出的家人也能恢復；
    - "parent"：其他一律只有本人能恢復——家族主群以外（只有本人的指令會寫 on 列）、管理者用 SQL 暫停的
      （'admin' 或任何不是帳號的字）、別名檔裡沒有的帳號暫停的。
    家族主群沒有列時暫停，列上的帳號是空的（set_consent：暫停不等於同意），只能比現在的帳號。"""
    row = ctx.consents.get(role) or {}
    by = str(row.get("changed_by") or "")
    if by and by in {str(row.get("user_id") or ""), str(ctx.roles.get(role) or "")} - {""}:
        return _SELF
    if not _default_on(ctx.group_id):
        return _PARENT_ONLY
    if not by or _known_member(by):
        return _BY_FAMILY
    return _PARENT_ONLY


def _resume_hint(ctx: _Ctx, role: str) -> str:
    rule = _resume_rule(ctx, role)
    if rule == _SELF:
        return f"{role}自己暫停的，要恢復請{role}本人打 /健康 恢復"
    if rule == _PARENT_ONLY:
        return f"要恢復請{role}本人打 /健康 恢復"
    return f"要恢復：打 /健康 恢復 {role}"


def _state_line(ctx: _Ctx, role: str) -> str:
    state = ctx.states[role]
    if state == _PAUSED:
        return f"已暫停記錄（{_resume_hint(ctx, role)}）"
    if state == _UNSET:
        return "還沒設定帳號"
    if _default_on(ctx.group_id):  # 任何人的下一則留言或 /健康 指令都會寫下預設列
        return "還沒開始記錄（下一則群組留言就會自動開始）"
    return f"還沒開始記錄（要{role}本人打 /健康 恢復）"


# ── 紀錄整理 ────────────────────────────────────────────────────────────────


def _events(ctx: _Ctx, role: str, since: date, until: date) -> list[dict]:
    """讀紀錄並在渲染前再驗一次固定詞表；詞表載不進來就當成沒有紀錄。"""
    vocab = parent_health_db.vocabulary()
    if vocab is None:
        return []
    rows = parent_health_db.list_events(
        ctx.group_id, role, since_day=since.isoformat(), until_day=until.isoformat(), db_path=ctx.db_path
    )
    out = []
    for row in rows:
        if not parent_health_db.record_allowed(vocab, row["category"], row["item"], row["value"], row["flag"]):
            continue
        try:
            day = date.fromisoformat(row["event_day"])
        except (TypeError, ValueError):
            continue
        out.append({**row, "day": day})
    return out


def _distinct_days(rows: list[dict], item: str, category: str | None = None) -> set[date]:
    return {
        row["day"]
        for row in rows
        if row["item"] == item and (category is None or row["category"] == category)
    }


def _ranked_by_days(rows: list[dict]) -> list[tuple[str, int]]:
    """[(標籤, 不同天數)]，天數多的在前，同天數取最近說的。"""
    days: dict[str, set[date]] = {}
    latest: dict[str, int] = {}
    for row in rows:
        days.setdefault(row["item"], set()).add(row["day"])
        latest[row["item"]] = max(latest.get(row["item"], 0), int(row["event_ts"]))
    return [
        (item, len(days[item]))
        for item in sorted(days, key=lambda item: (-len(days[item]), -latest[item], item))
    ]


def _latest_distinct(rows: list[dict], key) -> list[dict]:
    """依 key 去重，保留每組最新一筆；新到舊。"""
    best: dict = {}
    for row in rows:
        k = key(row)
        if k not in best or (row["event_ts"], row["event_id"]) >= (best[k]["event_ts"], best[k]["event_id"]):
            best[k] = row
    return sorted(best.values(), key=lambda row: (row["event_ts"], row["event_id"]), reverse=True)


@dataclass
class _Summary:
    role: str
    denom: int
    taken_days: int = 0
    forgot_days: int = 0
    pickup: bool = False
    stopped: bool = False
    other_med: list[str] = field(default_factory=list)
    visits_done: list[tuple[date, str]] = field(default_factory=list)
    exercise: list[tuple[str, int]] = field(default_factory=list)
    sleep: list[tuple[str, int]] = field(default_factory=list)
    diet: list[str] = field(default_factory=list)
    readings: list[tuple[str, str, date]] = field(default_factory=list)
    symptoms: list[tuple[str, int]] = field(default_factory=list)
    flags: list[tuple[date, str]] = field(default_factory=list)

    def has_med(self) -> bool:
        return bool(self.taken_days or self.forgot_days or self.pickup or self.stopped or self.other_med)


def _summary(ctx: _Ctx, role: str) -> _Summary:
    """近 7 天（D-6..D，台北日期）的整理。計數一律是「不同天數」：爸媽和兄弟姊妹回報同一件事
    不會算兩次。視窗從同意那天開始算（暫停又恢復時不把之前的天數算進來），分母＝min(7, 同意以來天數)。"""
    on_day = _local(ctx.consents[role]["on_since"]).date()
    start = max(ctx.today - timedelta(days=6), on_day)
    denom = max(1, min(7, (ctx.today - on_day).days + 1))
    rows = [row for row in _events(ctx, role, start, ctx.today) if row["event_ts"] <= ctx.now]
    summary = _Summary(role=role, denom=denom)
    by_category: dict[str, list[dict]] = {}
    for row in rows:
        by_category.setdefault(row["category"], []).append(row)

    med = by_category.get("med", [])
    summary.taken_days = len(_distinct_days(med, _MED_TAKEN))
    summary.forgot_days = len(_distinct_days(med, _MED_FORGOT))
    summary.pickup = any(row["item"] == _MED_PICKUP for row in med)
    summary.stopped = any(row["item"] == _MED_STOP for row in med)
    known_med = {_MED_TAKEN, _MED_FORGOT, _MED_PICKUP, _MED_STOP}
    summary.other_med = [row["item"] for row in _latest_distinct(
        [row for row in med if row["item"] not in known_med], lambda row: row["item"]
    )]

    summary.visits_done = [
        (row["day"], row["item"])
        for row in _latest_distinct(by_category.get("visit", []), lambda row: (row["item"], row["day"]))
    ]
    summary.exercise = _ranked_by_days(by_category.get("exercise", []))
    sleep = _ranked_by_days(by_category.get("sleep", []))
    order = {_SLEEP_BAD: 0, _SLEEP_GOOD: 1}
    summary.sleep = sorted(sleep, key=lambda pair: order.get(pair[0], 2))
    summary.diet = [row["item"] for row in _latest_distinct(by_category.get("diet", []), lambda row: row["item"])]

    body = by_category.get("body", [])
    summary.flags = [
        (row["day"], row["item"])
        for row in _latest_distinct(
            [row for row in body if row["flag"] == parent_health_db.REDFLAG], lambda row: row["item"]
        )
    ]
    summary.readings = [
        (row["item"], row["value"], row["day"])
        for row in _latest_distinct([row for row in body if row["value"]], lambda row: row["item"])
    ]
    summary.symptoms = _ranked_by_days(
        [row for row in body if not row["value"] and row["flag"] != parent_health_db.REDFLAG]
    )
    return summary


# ── 下次看診（從提醒讀，唯讀） ──────────────────────────────────────────────

_VISIT_KEYWORDS = (
    "回診", "複診", "看診", "門診", "看醫生", "看醫師", "看牙", "洗牙", "拔牙", "補牙", "看眼科",
    "看中醫", "針灸", "復健", "健檢", "抽血", "照胃鏡", "照X光", "超音波", "打疫苗",
)
_VISIT_KEYWORD_RE = re.compile("|".join(map(re.escape, sorted(_VISIT_KEYWORDS, key=len, reverse=True))))
# 同義詞顯示成抽取詞表裡的標籤（同一次看診的不同寫法去重時才會合在一起）
_VISIT_CANONICAL = {"複診": "回診", "看醫師": "看醫生", "門診": "看診"}
_TIME_PART = r"(?:\s*(?P<hour>[01]?\d|2[0-3])\s*[:：]\s*(?P<minute>[0-5]\d)(?!\d))?"
_MD_RES = (
    re.compile(
        r"(?<![\d/])(?:(?P<year>20\d{2})\s*/\s*)?(?P<month>1[0-2]|0?[1-9])\s*/\s*(?P<day>3[01]|[12]\d|0?[1-9])"
        r"(?![\d/])(?!\s*[顆粒錠包杯片匙])" + _TIME_PART  # 「藥吃 1/2 顆」是份量不是日期
    ),
    re.compile(
        r"(?<!\d)(?:(?P<year>20\d{2})\s*年\s*)?(?P<month>1[0-2]|0?[1-9])\s*月\s*(?P<day>3[01]|[12]\d|0?[1-9])"
        r"\s*[日號号]?" + _TIME_PART
    ),
)
_OFFSET_MARKER_RE = re.compile(r"[（(]?\s*(?:前一天|前\d+天|前[一二三四五六七]天|當天|提前)\s*提醒\s*[）)]?")
_KIN_BEFORE = "姑舅姨嬸伯叔乾大阿外祖岳公婆"
_BOTH_TITLES_RE = re.compile(r"爸爸媽媽|爸媽")
_FAMILY_TITLES = (
    (FATHER, re.compile(rf"(?<![{_KIN_BEFORE}])(?:爸爸|老爸|把拔|爸比|父親|爸)")),
    (MOTHER, re.compile(rf"(?<![{_KIN_BEFORE}])(?:媽媽|老媽|馬麻|媽咪|母親|媽)")),
)
# 家長自己建的提醒：「媽媽」「老婆」「你媽」是另一半或自己；「我爸」「我媽」「爸媽」是長輩，不算。
_PARENT_TITLES = {
    FATHER: (
        (MOTHER, re.compile(r"(?<![我外姑舅姨阿])媽媽|你們?媽|老婆|(?<=[我你])太太")),
        (FATHER, re.compile(r"(?<![我外姑舅姨阿])爸爸")),
    ),
    MOTHER: (
        (FATHER, re.compile(r"(?<![我外姑舅姨阿])爸爸|你們?爸|老公|(?<=[我你])先生")),
        (MOTHER, re.compile(r"(?<![我外姑舅姨阿])媽媽")),
    ),
}
# 姓氏緊接在爸爸／媽媽前面（王媽媽）、或「某人的」爸爸媽媽（小明的媽媽，我／你的除外）＝別人的爸媽。
# 只收常見姓氏，拿掉常當動詞或時間詞用的字（照顧、這周、連、何、鐘…），免得把「照顧媽媽」當成別人。
_SURNAMES = (
    "陳林黃張李王吳劉蔡楊許鄭謝洪郭邱曾廖賴徐葉蘇莊呂江蕭羅高潘簡朱鍾游彭詹胡施沈余盧梁趙顏柯翁魏孫"
    "戴范方宋鄧杜傅侯曹薛丁卓阮馬董溫唐藍石蔣古紀姚馮歐程湯黎田康姜白汪鄒尤巫涂龔嚴韓袁金童陸夏柳"
    "邵錢伍倪于譚駱熊任甘秦毛章史官萬俞雷饒闕凌崔尹孔辛武辜陶段龍韋葛池孟褚殷麥賀賈莫"
)
_SOMEONE_ELSES_TITLE_RE = re.compile(rf"(?:[{_SURNAMES}]|(?<![我你妳])的)(?:爸爸|媽媽|老爸|老媽|爸|媽)")
# 家長建的提醒寫的是別人（長輩、孫子、朋友、寵物）時，不退回「建立者本人」。
_OTHER_PERSON_RE = re.compile(
    r"我爸|我媽|外公|外婆|阿公|阿嬤|阿媽|爺爺|奶奶|岳父|岳母|公公|婆婆|孫子|孫女|小孩|孩子|兒子|女兒|"
    r"寶寶|朋友|同事|鄰居|狗|貓"
)


@dataclass(frozen=True)
class _Visit:
    role: str
    day: date
    clock: str  # "09:00" 或 ""
    keyword: str

    def when(self) -> str:
        return f"{_md_wd(self.day)}{self.clock + ' ' if self.clock else ''}{self.keyword}"


def _nearest_date(month: int, day: int, anchor: date) -> date | None:
    """沒寫年份的 M/D：取離提醒時間最近的那一年（12 月建 1 月的看診也對）。"""
    best = None
    for year in (anchor.year - 1, anchor.year, anchor.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if best is None or abs((candidate - anchor).days) < abs((best - anchor).days):
            best = candidate
    return best


def _parse_when(text: str, anchor: date) -> tuple[date, str] | None:
    for pattern in _MD_RES:
        match = pattern.search(text or "")
        if match is None:
            continue
        month, day_of_month = int(match.group("month")), int(match.group("day"))
        if match.group("year"):
            try:
                day = date(int(match.group("year")), month, day_of_month)
            except ValueError:
                continue
        else:
            day = _nearest_date(month, day_of_month, anchor)
        if day is None:
            continue
        clock = ""
        if match.group("hour") is not None:
            clock = f"{int(match.group('hour')):02d}:{match.group('minute')}"
        return day, clock
    return None


def _strip_offset_marker(action: str) -> str:
    try:
        import reminder_intent

        return reminder_intent.strip_reminder_offset_marker(action)
    except Exception:
        return re.sub(r"\s+", " ", _OFFSET_MARKER_RE.sub(" ", action)).strip(" ，,、:：")


def _offset_days(action: str) -> int | None:
    try:
        import reminder_intent

        return reminder_intent.reminder_offset_days(action)
    except Exception:
        return None


def _titles_in(text: str, creator_role: str) -> tuple[str, ...]:
    found: list[str] = []
    text = _SOMEONE_ELSES_TITLE_RE.sub(" ", text)  # 王媽媽、小明的爸爸：別人的
    if creator_role in _PARENT_TITLES:
        text = _BOTH_TITLES_RE.sub(" ", text)  # 家長說的「爸媽」是自己的爸媽
        for role, pattern in _PARENT_TITLES[creator_role]:
            if pattern.search(text) and role not in found:
                found.append(role)
        return tuple(found)
    if _BOTH_TITLES_RE.search(text):
        found += [FATHER, MOTHER]
        text = _BOTH_TITLES_RE.sub(" ", text)
    for role, pattern in _FAMILY_TITLES:
        if pattern.search(text) and role not in found:
            found.append(role)
    return tuple(role for role in PARENTS if role in found)


def _visit_roles(row: dict, body: str, roles: dict, mapping: dict, aliases: dict) -> tuple[str, ...]:
    """這筆提醒是誰的看診：提醒標的對象（只標建立者自己的不算）→ 內容裡的家長稱謂（依建立者
    解讀）→ 標了全家 → 建立者本人是家長。「@全家 10/23 帶媽媽回診」只算媽媽。"""
    import line_mentions

    creator = str(row.get("user_id") or "")
    role_by_uid = {uid: role for role, uid in roles.items() if uid}
    names = [_clean_alias(name) for name in (row.get("mention_aliases") or []) if _clean_alias(name)]
    found: list[str] = []
    mentions_someone_else = False  # 標了建立者以外、不是爸媽的家人
    for name in names:
        if line_mentions.is_all_participants([name]):
            continue  # 全家：內容沒寫是誰時才算兩位
        canonical = _clean_alias(mapping.get(name, name))
        uids = {uid for uid, label in aliases.items() if label == canonical}
        role = None
        if len(uids) == 1:
            uid = next(iter(uids))
            if uid == creator:
                continue  # 前一天／當天提醒都標建立者自己：改看稱謂
            role = role_by_uid.get(uid)
        elif not uids and canonical in PARENTS:
            role = canonical
        if role and role not in found:
            found.append(role)
        elif role is None:
            mentions_someone_else = True
    if found:
        return tuple(role for role in PARENTS if role in found)
    titled = _titles_in(body, role_by_uid.get(creator, FAMILY))
    if titled:
        return titled
    if names and line_mentions.is_all_participants(names):
        return PARENTS
    creator_role = role_by_uid.get(creator)
    if (
        creator_role is None
        or mentions_someone_else
        or _OTHER_PERSON_RE.search(body)
        or _SOMEONE_ELSES_TITLE_RE.search(body)
        or _BOTH_TITLES_RE.search(body)  # 家長寫的「爸媽」是自己的爸媽
    ):
        return ()
    return (creator_role,)


def _upcoming_visits(ctx: _Ctx) -> list[_Visit]:
    """未來的看診（今天還沒過的也算），從提醒讀（list_pending_reminders_full(dedupe=False) 只讀不寫）。
    只輸出解析出的日期／時間＋固定看診詞，不輸出提醒內容。以 (家長, 日期, 看診詞) 去重。"""
    import line_mentions
    import memory

    rows = memory.list_pending_reminders_full(ctx.group_id, dedupe=False)
    aliases = {
        uid: _clean_alias(label)
        for uid, label in line_mentions.load_user_aliases().items()
        if isinstance(uid, str) and uid
    }
    try:
        mapping = {
            _clean_alias(k): _clean_alias(v)
            for k, v in line_mentions.configured_family_alias_mapping(include_short=False).items()
        }
    except Exception:
        mapping = {}
    now_clock = _hhmm(ctx.now)
    visits: dict[tuple[str, date, str], _Visit] = {}
    for row in rows:
        raw_action = str(row.get("action") or "")
        action = _strip_offset_marker(raw_action)
        creator_alias = aliases.get(str(row.get("user_id") or ""), "")
        body = action
        if creator_alias and body.startswith(creator_alias):
            body = body[len(creator_alias):]  # 前一天／當天提醒的開頭是建立者的稱呼
        keyword_match = _VISIT_KEYWORD_RE.search(body)
        if keyword_match is None:
            continue
        try:
            remind_at = int(row.get("remind_at") or 0)
        except (TypeError, ValueError):
            remind_at = 0
        anchor = _local(remind_at).date() if remind_at > 0 else ctx.today
        parsed = _parse_when(body, anchor) or _parse_when(str(row.get("source_text") or ""), anchor)
        if parsed is not None:
            day, clock = parsed
        elif remind_at > 0:
            offset = _offset_days(raw_action)
            moment = _local(remind_at)
            if offset is not None:  # 前 N 天／當天提醒：提醒時間不是看診時間
                day, clock = moment.date() + timedelta(days=offset), ""
            else:
                day = moment.date()
                clock = moment.strftime("%H:%M")
                if row.get("time_kind") != "clock" or clock == "00:00":  # 全天鏡像、沒講時間
                    clock = ""
        else:
            continue
        if day < ctx.today or (day == ctx.today and clock and clock < now_clock):
            continue
        keyword = _VISIT_CANONICAL.get(keyword_match.group(0), keyword_match.group(0))
        for role in _visit_roles(row, body, ctx.roles, mapping, aliases):
            key = (role, day, keyword)
            current = visits.get(key)
            if current is None or (clock and not current.clock):
                visits[key] = _Visit(role, day, clock, keyword)
    return sorted(visits.values(), key=lambda v: (v.day, v.clock or "99:99", PARENTS.index(v.role), v.keyword))


def _safe_upcoming(ctx: _Ctx) -> list[_Visit]:
    try:
        return [visit for visit in _upcoming_visits(ctx) if ctx.states.get(visit.role) == _ON]
    except Exception as exc:
        _err("visit lookup", exc)
        return []


# ── 卡片 ────────────────────────────────────────────────────────────────────

_GRAY = "#555555"
_TIP_COLOR = "#1E5AA8"
_BUTTON_BG = "#E8F0FE"


def _text(text: str, *, size: str = "lg", weight: str | None = None, color: str | None = None) -> dict:
    node = {"type": "text", "text": text, "size": size, "wrap": True}
    if weight:
        node["weight"] = weight
    if color:
        node["color"] = color
    return node


def _separator() -> dict:
    return {"type": "separator", "margin": "md"}


def _button(label: str, command: str) -> dict:
    """大字按鈕：Flex 的 button 不能調字級，照大字選單做成可以點的 box。"""
    return {
        "type": "box",
        "layout": "vertical",
        "backgroundColor": _BUTTON_BG,
        "cornerRadius": "lg",
        "paddingAll": "lg",
        "action": {"type": "message", "label": label, "text": command},
        "contents": [
            {
                "type": "text",
                "text": label,
                "size": "xl",
                "weight": "bold",
                "color": "#111111",
                "align": "center",
                "wrap": True,
            }
        ],
    }


def _bubble(title: str, subtitle: str | None, body: list[dict], footer: list[dict]) -> dict:
    header = [_text(title, size="xxl", weight="bold")]
    if subtitle:
        header.append(_text(subtitle, size="lg", color=_GRAY))
    return {
        "type": "bubble",
        "size": "giga",
        "header": {"type": "box", "layout": "vertical", "contents": header},
        "body": {"type": "box", "layout": "vertical", "spacing": "md", "contents": body},
        "footer": {"type": "box", "layout": "vertical", "spacing": "md", "contents": footer},
    }


def _card_texts(node) -> list[str]:
    """卡片上所有會被看到或送出的字；同時檢查按鈕標籤與送出文字的長度（超過就丟例外）。"""
    texts: list[str] = []
    if isinstance(node, dict):
        action = node.get("action")
        if isinstance(action, dict):
            label, command = str(action.get("label") or ""), str(action.get("text") or "")
            if action.get("type") != "message" or not 0 < len(label) <= _LABEL_MAX:
                raise ValueError("card action label")
            if not 0 < len(command) <= _ACTION_TEXT_MAX:
                raise ValueError("card action text")
            texts += [label, command]
        if node.get("type") == "text":
            texts.append(str(node.get("text") or ""))
        for key, value in node.items():
            if key != "action":
                texts += _card_texts(value)
    elif isinstance(node, list):
        for item in node:
            texts += _card_texts(item)
    return texts


def _card_reply(alt_text: str, card: dict) -> CommandReply:
    """送出前自己驗一次（altText 與每個字原樣通過 outbound validator、按鈕長度、JSON 大小）；
    沒過就回固定句（log 只記種類）。"""
    try:
        import output_validator

        if not alt_text.startswith(CARD_PREFIX) or len(alt_text) > _ALT_MAX:
            raise ValueError("card alt text")
        if len(json.dumps(card, ensure_ascii=False).encode("utf-8")) > _CARD_MAX_BYTES:
            raise ValueError("card too large")
        for value in [alt_text, *_card_texts(card)]:
            result = output_validator.validate_outbound_text(value)
            if not result.ok or result.text != value:
                raise ValueError("card text rejected by outbound validator")
    except Exception as exc:
        _err("card check", exc)
        return CommandReply(text=FAILURE_TEXT)
    return CommandReply(card=card, alt_text=alt_text)


def _med_line(s: _Summary) -> str:
    if not s.has_med():
        return _EMPTY
    # 只說了忘了／拿藥／停藥的時候不寫「有說吃藥 0/7 天」（讀起來像七天都沒吃）。
    parts = [f"有說吃藥 {s.taken_days}/{s.denom} 天"] if s.taken_days else []
    if s.forgot_days:
        parts.append(f"忘了 {s.forgot_days} 天")
    if s.pickup:
        parts.append(_MED_PICKUP)
    if s.stopped:
        parts.append(_MED_STOP)
    parts += s.other_med[:2]
    return "、".join(parts)


def _visit_line(s: _Summary, visits: list[_Visit]) -> str:
    parts = [f"下次 {visits[0].when()}"] if visits else []
    parts += [f"{_md(day)} {item}" for day, item in s.visits_done[:2]]
    return "；".join(parts) or _EMPTY


def _days_list(pairs: list[tuple[str, int]], limit: int) -> str:
    return "、".join(f"{item} {days} 天" for item, days in pairs[:limit])


def _body_line(s: _Summary) -> str:
    parts = [f"⚠️ {_md(day)} 提到{label}" for day, label in s.flags[:2]]
    parts += [f"{item} {value}（{_md(day)}）" for item, value, day in s.readings[:4]]
    parts += [f"{item} {days} 天" for item, days in s.symptoms[:3]]
    return "、".join(parts) or _EMPTY


def _overview_rows(s: _Summary, visits: list[_Visit]) -> list[str]:
    return [
        f"💊 用藥：{_med_line(s)}",
        f"🏥 看診：{_visit_line(s, visits)}",
        f"🚶 運動：{_days_list(s.exercise, 2) or _EMPTY}",
        f"😴 睡眠：{_days_list(s.sleep, 2) or _EMPTY}",
        f"🍚 飲食：{'、'.join(s.diet[:2]) or _EMPTY}",
        f"🩺 身體：{_body_line(s)}",
    ]


def _tip(summaries: dict[str, _Summary], visits: list[_Visit], today: date) -> str | None:
    """💡 最多一行、固定樣板、只講照顧安排：近 3 天緊急字詞 > 7 天內看診 > 這週忘了吃藥 ≥ 2 天。"""
    for role in PARENTS:
        s = summaries.get(role)
        recent = [(day, label) for day, label in (s.flags if s else []) if day >= today - timedelta(days=2)]
        if recent:
            day, label = recent[0]
            return f"💡 {role} {_md(day)} 提到{label}，可以問問{_PRONOUN[role]}現在的狀況"
    for visit in visits:
        if visit.role in summaries and visit.day <= today + timedelta(days=7):
            return f"💡 {visit.role} {_md_wd(visit.day)}{visit.keyword}，可以先說好誰陪"
    for role in PARENTS:
        s = summaries.get(role)
        if s and s.forgot_days >= 2:
            return f"💡 {role}這週說忘了吃藥 {s.forgot_days} 天，可以試試分格藥盒或手機鬧鐘"
    return None


def _overview(ctx: _Ctx) -> CommandReply:
    visits = _safe_upcoming(ctx) if _ON in ctx.states.values() else []
    summaries: dict[str, _Summary] = {}
    body: list[dict] = []
    for index, role in enumerate(PARENTS):
        if index:
            body.append(_separator())
        body.append(_text(role, size="xl", weight="bold"))
        state = ctx.states[role]
        if state != _ON:
            body.append(_text(_state_line(ctx, role), color=_GRAY))
            continue
        summaries[role] = summary = _summary(ctx, role)
        body += [_text(line) for line in _overview_rows(summary, [v for v in visits if v.role == role])]
    tip = _tip(summaries, visits, ctx.today)
    if tip:
        body += [_separator(), _text(tip, weight="bold", color=_TIP_COLOR)]
    on_roles = [role for role in PARENTS if ctx.states[role] == _ON]
    # 沒有要爸媽本人按的按鈕（Andrew 10/17：「他們不會去按」）
    footer = [_button(f"看{role}細節", f"/健康 {role}") for role in on_roles]
    if on_roles:
        footer.append(_button("📋 健康分析", "/健康 分析"))
    footer += [_button("📖 怎麼用", "/健康 說明"), _text(_CARD_NOTE, size="md", color=_GRAY)]
    subtitle = f"近 7 天（{_md(ctx.today - timedelta(days=6))}–{_md(ctx.today)}）"
    return _card_reply(_OVERVIEW_ALT, _bubble("爸媽健康", subtitle, body, footer))


def _reporter_label(user_id: str) -> str:
    """回報者：只用本機別名（不用 LINE 顯示名稱，誰都能改名）；沒有或字元不單純就寫「家人」。"""
    try:
        import line_mentions

        alias = _clean_alias(line_mentions.alias_for_user_id(user_id or ""))
    except Exception:
        alias = ""
    if alias and re.fullmatch(r"[一-鿿A-Za-z0-9]{1,10}", alias):
        return alias
    return "家人"


def _item_text(row: dict) -> str:
    if row["flag"] == parent_health_db.REDFLAG:
        return f"⚠️ {row['item']}"
    if row["value"]:
        return f"{row['item']} {row['value']}"
    return row["item"]


def _detail_lines(rows: list[dict], self_user_id: str) -> list[str]:
    """一天一行、新到舊：「10/11（六）吃藥、散步、血壓 135/85（家人回報）」；家長本人說的不標。"""
    days: dict[date, dict[tuple, dict]] = {}
    for row in sorted(rows, key=lambda row: (row["event_ts"], row["event_id"])):
        key = (row["category"], row["item"], row["value"], row["flag"])
        entry = days.setdefault(row["day"], {}).setdefault(
            key, {"row": row, "self": False, "reporter": row["reporter_user_id"]}
        )
        if row["reporter_user_id"] and row["reporter_user_id"] == self_user_id:
            entry["self"] = True
    lines = []
    labels: dict[str, str] = {}
    for day in sorted(days, reverse=True):
        parts = []
        for entry in list(days[day].values())[:12]:
            text = _item_text(entry["row"])
            if not entry["self"]:
                reporter = entry["reporter"]
                if reporter not in labels:
                    labels[reporter] = _reporter_label(reporter)
                text += f"（{labels[reporter]}回報）"
            parts.append(text)
        lines.append(f"{_md_wd(day)}{'、'.join(parts)}")
    return lines


def _detail(ctx: _Ctx, role: str) -> CommandReply:
    state = ctx.states[role]
    if state == _ON:
        rows = _events(ctx, role, ctx.today - timedelta(days=13), ctx.today)
        lines = _detail_lines(rows, ctx.consents[role]["user_id"])
        body = [_text(line) for line in lines] or [_text("近 14 天沒有紀錄")]
    else:
        body = [_text(_state_line(ctx, role), color=_GRAY)]
    footer = [
        _text("記錯了：長按那則留言→回覆→打 /健康 刪除", size="md", color=_GRAY),
        _text(f"要暫停：打 /健康 暫停 {role}", size="md", color=_GRAY),
        _text(_CARD_NOTE, size="md", color=_GRAY),
        _button("回爸媽健康卡", "/健康"),
    ]
    return _card_reply(_DETAIL_ALT, _bubble(f"{role}近 14 天", "新到舊，一天一行", body, footer))


# ── 分析卡（/健康 分析，2026-10-17） ───────────────────────────────────────


def _analysis_rows(ctx: _Ctx, role: str) -> list[dict]:
    """這次開始記錄以來、到現在為止的紀錄；體重規則看 180 天，所以讀到 D-179。"""
    on_since = ctx.consents[role]["on_since"]
    on_day = _local(on_since).date()
    rows = _events(ctx, role, max(ctx.today - timedelta(days=179), on_day), ctx.today)
    return [row for row in rows if on_since <= row["event_ts"] <= ctx.now]


def _role_visits(visits: list[_Visit], role: str) -> list[tuple[date, str]]:
    return [(visit.day, visit.keyword) for visit in visits if visit.role == role]


def _analysis(ctx: _Ctx) -> CommandReply:
    """近 14 天的分析卡：每位家長最多 3 條（緊急的一律列），建議另起一行；不寫 119（當下怎麼做只在私訊）。"""
    try:
        import parent_health_analysis as analysis
    except Exception as exc:  # 新模組壞了只影響這張卡，/健康 其他功能和 119 照常
        _err("analysis import", exc)
        return _reply(FAILURE_TEXT)
    visits = _safe_upcoming(ctx) if _ON in ctx.states.values() else []
    start = ctx.today - timedelta(days=13)
    body: list[dict] = []
    for index, role in enumerate(PARENTS):
        if index:
            body.append(_separator())
        body.append(_text(role, size="xl", weight="bold"))
        state = ctx.states[role]
        if state != _ON:
            body.append(_text(_state_line(ctx, role), color=_GRAY))
            continue
        on_day = _local(ctx.consents[role]["on_since"]).date()
        if on_day > start:
            body.append(_text(f"從 {_md(on_day)} 開始記錄", size="md", color=_GRAY))
        rows = _analysis_rows(ctx, role)
        shown, hidden = analysis.card_lines(
            analysis.analyze(rows, today=ctx.today, visits=_role_visits(visits, role), pronoun=_PRONOUN[role])
        )
        if not shown:
            recent = any(row["day"] >= start for row in rows)
            body.append(_text("依目前的提醒規則，這段時間沒有要提醒的項目" if recent else "近 14 天沒有紀錄"))
            continue
        for finding in shown:
            body.append(_text(f"{analysis.TONE_EMOJI.get(finding.tone, '📝')} {finding.card}"))
            if finding.advice:
                body.append(_text(f"→ {finding.advice}", size="md", color=_TIP_COLOR))
        if hidden:
            body.append(_text(f"另外還有 {hidden} 項，完整整理每週日私訊 Andrew", size="md", color=_GRAY))
    on_roles = [role for role in PARENTS if ctx.states[role] == _ON]
    footer = [_button(f"看{role}細節", f"/健康 {role}") for role in on_roles]
    footer += [
        _button("回爸媽健康卡", "/健康"),
        _text(_ANALYSIS_NOTE, size="md", color=_GRAY),
        _text(_ANALYSIS_DM_NOTE, size="md", color=_GRAY),
    ]
    subtitle = f"近 14 天（{_md(start)}–{_md(ctx.today)}），只看群組有提到的"
    return _card_reply(_ANALYSIS_ALT, _bubble("📋 爸媽健康分析", subtitle, body, footer))


# ── 指令 ────────────────────────────────────────────────────────────────────


def _parse_command(body: str) -> tuple[str, str | None]:
    compact = re.sub(r"\s+", "", _normalize(body))
    if not compact:
        return "overview", None
    if compact in PARENTS:
        return "detail", compact
    if compact == "分析":
        return "analysis", None
    if compact in ("恢復", "同意"):  # 「同意」是 10/17 前舊卡片上的按鈕
        return "resume", None
    if compact.startswith("恢復") and compact[2:] in PARENTS:  # 家人恢復家人暫停的
        return "resume", compact[2:]
    if compact == "暫停":
        return "pause_self", None
    if compact.startswith("暫停") and compact[2:] in PARENTS:
        return "pause", compact[2:]
    if compact in _BUTTON_RECORDS:
        return "record", compact
    if compact == "刪除":
        return "delete_quoted", None
    if compact == "刪除我的紀錄":
        return "delete_mine", None
    return "help", None  # 說明、「同意 爸爸」、其他


def _reply(text: str) -> CommandReply:
    return CommandReply(text=text)


def _resume(ctx: _Ctx, user_id: str | None, target: str | None) -> CommandReply:
    """「/健康 恢復」（舊卡片的「/健康 同意」也走這裡）：本人恢復自己的紀錄；「/健康 恢復 爸爸」：家人恢復
    家人暫停的（_resume_rule）。家族主群預設就在記錄，只有暫停過的才要恢復；其他群組要本人打這個才開始記錄。"""
    role = _role_from(ctx.roles, user_id)
    if target is not None and target != role:
        return _resume_for(ctx, target, user_id)
    if role == FAMILY:
        return _family_resume_hint(ctx)
    if ctx.states[role] == _ON:  # 重按不重設開始時間（分母、17 天門檻照舊）
        others = "".join(f"；{r}{_state_line(ctx, r)}" for r in PARENTS if r != role and ctx.states[r] == _PAUSED)
        return _reply(f"爸媽健康：{role}的健康紀錄正在記錄，不用按。要暫停打 /健康 暫停{others}")
    parent_health_db.set_consent(
        ctx.group_id, role, "on", user_id=user_id, changed_by=user_id, now=ctx.now, db_path=ctx.db_path
    )
    return _reply(
        f"爸媽健康：已恢復記錄{role}的健康留言。\n{_RECORDING_EXPLANATION}\n"
        "要暫停：打 /健康 暫停；要刪掉自己的全部紀錄：打 /健康 刪除我的紀錄"
    )


def _family_resume_hint(ctx: _Ctx) -> CommandReply:
    """家人打 /健康 恢復（沒指定誰）：說清楚能恢復誰、怎麼打；不替任何人恢復、不寫入。"""
    paused = [r for r in PARENTS if ctx.states[r] == _PAUSED]
    family_ok = [r for r in paused if _resume_rule(ctx, r) == _BY_FAMILY]
    if len(family_ok) == 1:
        return _reply(f"爸媽健康：要恢復{family_ok[0]}：打 /健康 恢復 {family_ok[0]}")
    if family_ok:
        return _reply("爸媽健康：要恢復誰？打 /健康 恢復 爸爸 或 /健康 恢復 媽媽")
    if paused:
        return _reply("爸媽健康：" + "；".join(f"{r}{_state_line(ctx, r)}" for r in paused))
    unset = [r for r in PARENTS if ctx.roles.get(r) is None]
    if unset:
        logger.warning(
            "parent health resume attempted with role not configured roles=%s",
            ",".join(_ROLE_LOG[r] for r in unset),
        )
        return _reply(f"爸媽健康：還沒設定{'、'.join(unset)}是哪個 LINE 帳號，設定好才能記錄")
    stopped = [r for r in PARENTS if ctx.states[r] != _ON]
    if not stopped:
        return _reply("爸媽健康：爸爸、媽媽的健康紀錄都在記錄中，不用按")
    if _default_on(ctx.group_id):
        return _reply(f"爸媽健康：{'、'.join(stopped)}的紀錄會從下一則群組留言自動開始")
    return _reply(f"爸媽健康：要{'、'.join(stopped)}本人打 /健康 恢復 才會記錄")


def _resume_for(ctx: _Ctx, role: str, user_id: str | None) -> CommandReply:
    """家人（或另一位家長、沒帶帳號的事件）恢復 role：只恢復 _resume_rule＝family 的；其他都不替他做。"""
    state = ctx.states[role]
    if state == _ON:
        return _reply(f"爸媽健康：{role}的健康紀錄正在記錄，不用打")
    if state == _UNSET:
        return _reply(f"爸媽健康：還沒設定{role}是哪個 LINE 帳號，設定好才能記錄")
    if state != _PAUSED:
        if _default_on(ctx.group_id):
            return _reply(f"爸媽健康：{role}的紀錄會從下一則群組留言自動開始")
        return _reply(f"爸媽健康：要{role}本人打 /健康 恢復 才會記錄")
    rule = _resume_rule(ctx, role)
    if rule == _SELF:
        return _reply(f"爸媽健康：{role}自己暫停的，要{role}本人打 /健康 恢復")
    if rule == _PARENT_ONLY:
        return _reply(f"爸媽健康：要{role}本人打 /健康 恢復 才會記錄")
    # 停下來誰都能做；重新開始記錄要認得出是誰（沒帶帳號、別名檔裡沒有的都不行）
    if not user_id:
        return _reply(f"爸媽健康：看不出是誰打的，請用手機 LINE 再打一次 /健康 恢復 {role}")
    if not _known_member(user_id):
        return _reply(f"爸媽健康：要{role}本人或家人打 /健康 恢復 {role} 才會恢復")
    parent_health_db.set_consent(
        ctx.group_id, role, "on", user_id=ctx.roles[role], changed_by=user_id, now=ctx.now, db_path=ctx.db_path
    )
    _send_dm_background(f"🩺 爸媽健康：{role}的健康紀錄被家人恢復了（{_hhmm(ctx.now)}），群組 119 提醒也一起恢復。")
    return _reply(f"爸媽健康：已恢復記錄{role}的健康留言（從現在開始，暫停期間的留言不補記）。要暫停：打 /健康 暫停 {role}")


def _pause(ctx: _Ctx, role: str, user_id: str | None) -> CommandReply:
    row = ctx.consents.get(role)
    # 家族主群預設在記錄：還沒有同意列（例如預設列寫失敗）也直接寫成暫停
    if row is None and not _default_on(ctx.group_id):
        return _reply(f"爸媽健康：{role}還沒開始記錄，不用暫停")
    if row is not None and row.get("status") != "on":
        if _role_from(ctx.roles, user_id) == role and _resume_rule(ctx, role) != _SELF:
            # 家人先暫停、本人再打暫停：改成本人自己暫停的，之後只有本人能恢復（不私訊：本人的動作）
            parent_health_db.set_consent(
                ctx.group_id, role, "paused", user_id=row.get("user_id") or "", changed_by=user_id,
                now=ctx.now, db_path=ctx.db_path,
            )
            return _reply(f"爸媽健康：{role}的健康紀錄已經暫停了；現在改成{role}自己暫停的，之後只有{role}本人能恢復")
        return _reply(f"爸媽健康：{role}的健康紀錄已經暫停了")
    parent_health_db.set_consent(
        ctx.group_id, role, "paused", user_id=(row or {}).get("user_id") or "", changed_by=user_id or "",
        now=ctx.now, db_path=ctx.db_path,
    )
    # 寫完的暫停列（照 set_consent：帳號、on_since 保留，沒有列時帳號是空的）決定誰能恢復
    paused_row = {**(row or {"user_id": "", "on_since": 0}), "status": "paused", "changed_by": user_id or ""}
    consents = {**ctx.consents, role: paused_row}
    after = replace(ctx, consents=consents, states=_states(ctx.roles, consents))
    if _resume_rule(after, role) == _BY_FAMILY:
        how = f"要恢復：打 /健康 恢復 {role}"
    else:
        how = f"要恢復，請{role}本人打 /健康 恢復"
    if _role_from(ctx.roles, user_id) != role:  # 別人幫忙暫停：119 提醒也停了，私訊 Andrew
        _send_dm_background(
            f"🩺 爸媽健康：{role}的健康紀錄被家人暫停了（{_hhmm(ctx.now)}），群組 119 提醒也一起停；{how}。"
        )
    return _reply(
        f"爸媽健康：已暫停{role}的健康紀錄，卡片、週報段落、私訊和 119 提醒也一起停；已記的紀錄保留。{how}"
    )


def _record(ctx: _Ctx, user_id: str | None, key: str, message_id: str) -> CommandReply:
    """舊卡片上的「我吃藥了／我有運動」（10/17 起新卡片不再有）：爸媽本人按了照樣記一筆（指令不進留言抽取，
    不記就丟了）；家人按回固定句。"""
    role = _role_from(ctx.roles, user_id)
    if role == FAMILY:  # 有家長不在記錄中，順便說一聲（不然「就會自動記下」不準）
        notes = "；".join(f"{r}{_state_line(ctx, r)}" for r in PARENTS if ctx.states[r] != _ON)
        return _reply(f"{_AUTO_RECORD_TEXT}。{notes}" if notes else _AUTO_RECORD_TEXT)
    if ctx.states[role] != _ON:
        return _reply(f"爸媽健康：{role}{_state_line(ctx, role)}")
    category, item = _BUTTON_RECORDS[key]
    if not parent_health_db.is_allowed(category, item):
        logger.error("parent health button item not in vocabulary")
        return _reply(FAILURE_TEXT)
    written = parent_health_db.add_events(
        ctx.group_id,
        [_Record(role, category, item)],
        message_id=message_id,
        reporter_user_id=user_id,
        event_ts=ctx.now,
        source="button",
        now=ctx.now,
        db_path=ctx.db_path,
    )
    return _reply(_RECORDED_TEXT if written else "爸媽健康：這一筆已經記過了")


def _delete_quoted(ctx: _Ctx, quoted_message_id: str | None) -> CommandReply:
    if not quoted_message_id:
        return _reply("爸媽健康：要先長按那則留言→回覆，再打 /健康 刪除")
    deleted = parent_health_db.delete_message_events(ctx.group_id, quoted_message_id, db_path=ctx.db_path)
    if deleted:
        return _reply(f"爸媽健康：已刪掉那則留言記下的 {deleted} 筆紀錄")
    return _reply("爸媽健康：那則留言沒有記下健康紀錄")


def _delete_mine(ctx: _Ctx, user_id: str | None) -> CommandReply:
    role = _role_from(ctx.roles, user_id)
    if role == FAMILY:
        return _reply("爸媽健康：只有爸爸或媽媽本人能刪自己的全部紀錄")
    deleted = parent_health_db.delete_role_events(ctx.group_id, role, db_path=ctx.db_path)
    # 家族主群預設在記錄：刪完之後的留言還會繼續記，講清楚怎麼停
    still = "；之後的留言還會繼續記錄，要停請打 /健康 暫停" if ctx.states.get(role) == _ON else ""
    if deleted:
        return _reply(f"爸媽健康：已刪掉{role}的 {deleted} 筆紀錄{still}")
    return _reply(f"爸媽健康：{role}目前沒有紀錄{still}")


def handle_command(
    group_id: str,
    user_id: str | None,
    body: str,
    *,
    message_id: str,
    quoted_message_id: str | None = None,
    now: float | None = None,
) -> CommandReply:
    """「/健康」後面的字（main 已 NFKC、strip）→ 卡片或文字。卡片 altText 以 CARD_PREFIX 開頭、
    文字以 REPLY_PREFIX 開頭；出錯回 FAILURE_TEXT。"""
    try:
        if not group_id:
            return _reply(FAILURE_TEXT)
        command, arg = _parse_command(body)
        if command == "help":
            return _reply(_HELP_TEXT)
        ctx = _context(group_id, _now(now), ensure_default=True)
        if command == "overview":
            return _overview(ctx)
        if command == "detail":
            return _detail(ctx, arg)
        if command == "analysis":
            return _analysis(ctx)
        if command == "resume":
            return _resume(ctx, user_id, arg)
        if command == "pause_self":
            role = _role_from(ctx.roles, user_id)
            if role == FAMILY:
                return _reply("爸媽健康：要暫停誰？打 /健康 暫停 爸爸 或 /健康 暫停 媽媽")
            return _pause(ctx, role, user_id)
        if command == "pause":
            return _pause(ctx, arg, user_id)
        if command == "record":
            return _record(ctx, user_id, arg, str(message_id or ""))
        if command == "delete_quoted":
            return _delete_quoted(ctx, quoted_message_id)
        if command == "delete_mine":
            return _delete_mine(ctx, user_id)
        return _reply(_HELP_TEXT)
    except Exception as exc:
        _err("command", exc)
        return _reply(FAILURE_TEXT)


def is_health_card_text(text: str) -> bool:
    """給引用檢查：bot 存檔的健康卡（altText）以 CARD_PREFIX 開頭。"""
    try:
        return isinstance(text, str) and text.lstrip().startswith(CARD_PREFIX)
    except Exception:
        return False


# ── 緊急字詞（即時） ────────────────────────────────────────────────────────


def _recently_replied(group_id: str, role: str, now: float) -> bool:
    """這位家長 30 分鐘內有沒有成功回過 119 提醒（只讀）。"""
    last = parent_health_db.last_alert(group_id, role, "reply")
    return last is not None and now - last["sent_at"] < _REPLY_WINDOW_S


def red_flag_decision(
    group_id: str,
    user_id: str | None,
    text: str,
    *,
    message_id: str,
    event_ts_ms,
    now: float | None = None,
) -> RedFlagDecision | None:
    """記錄中的家長（家族主群預設）提到緊急狀況時回決定；其他回 None。

    在 webhook 鎖裡跑：先做純子字串預檢，沒命中就不碰別名檔與 DB；DB 只讀、不拿寫入鎖；不寫、不連網。
    - 先看未來句再看已在處理：未來句裡的急診／住院／送醫／救護車（排好的看診、住院）整筆丟掉，不私訊；
      其他未來句只私訊。
    - 要回群組的家長在去重之後才選：第一位 30 分鐘內沒回過的（「爸爸剛剛胸口好痛，媽媽也跌倒了」，
      爸爸剛回過也照樣為媽媽回）。私訊照每位家長各自的結果寫。
    - 同一則重送（message_id 相同）不會再回：回覆成功時這則提到的每位家長都記了 30 分鐘去重，
      重送一定在 30 分鐘內，超過 10 分鐘的又只私訊。
    """
    try:
        extract = _extract()
        if not isinstance(text, str) or not text or not extract.might_have_red_flag(text):
            return None
        if _is_command_text(text):
            return None
        consents = parent_health_db.consent_states(group_id)
        default_on = _default_on(group_id)
        if not default_on and not any(row.get("status") == "on" for row in consents.values()):
            return None
        now_f = _now(now)
        event_ts = _event_ts(event_ts_ms, now_f)
        roles = _resolve_roles()
        states = _states(roles, consents)
        if default_on:
            # 家族主群預設在記錄：預設列還沒寫（或帳號剛換人）的家長也算；這裡只讀不寫
            states = {role: (_ON if state in (_NONE, _RECONSENT) else state) for role, state in states.items()}
        in_care_labels = set(getattr(extract, "IN_CARE_LABELS", ()))
        allowed_labels = set(getattr(extract, "RED_FLAG_LABELS", ())) | in_care_labels
        hits: list[tuple[str, str, str]] = []  # (家長, 標籤, reply／in_care／future)
        names = _name_table(roles)
        for flag in extract.detect_red_flags(
            _with_parent_titles(text, names), _role_from(roles, user_id), other_names=_other_names(names)
        ):
            role, label = getattr(flag, "role", None), getattr(flag, "label", None)
            if role not in PARENTS or label not in allowed_labels or not getattr(flag, "urgent", False):
                continue
            if states.get(role) != _ON or event_ts < consents.get(role, {}).get("on_since", 0):
                continue
            if getattr(flag, "future", False):
                if label in in_care_labels:
                    continue  # 「下週要住院開刀」「明天去急診拿報告」：不是緊急狀況
                kind = "future"
            elif getattr(flag, "in_care", False):
                kind = "in_care"
            else:
                kind = "reply"
            if not any(hit[:2] == (role, label) for hit in hits):
                hits.append((role, label, kind))
        if not hits:
            return None
        replyable = list(dict.fromkeys(role for role, _label, kind in hits if kind == "reply"))
        too_old = now_f - event_ts > _EVENT_MAX_AGE_S
        reply_role = None
        if replyable and not too_old:
            reply_role = next((role for role in replyable if not _recently_replied(group_id, role, now_f)), None)

        def reason_of(role: str) -> str:
            kinds = {kind for hit_role, _label, kind in hits if hit_role == role}
            if "reply" in kinds:
                if reply_role is not None:
                    return "send"  # 這次的群組提醒也涵蓋這位
                return "old_event" if too_old else "deduped"
            return "in_care" if "in_care" in kinds else "future"

        role = reply_role or (replyable[0] if replyable else hits[0][0])
        labels = tuple(label for hit_role, label, _kind in hits if hit_role == role)
        # 另一位家長（同一則一起提到）：標籤和他自己的結果只進私訊（靜音時私訊是唯一的通知）
        others = tuple((hit_role, label, reason_of(hit_role)) for hit_role, label, _kind in hits if hit_role != role)
        today_start = _day_start_ts(_today(now_f))
        allow_push = (
            parent_health_db.count_alerts(group_id, "reply", since_ts=today_start) < _MAX_PUSH_REPLIES_PER_DAY
        )
        return RedFlagDecision(
            role, labels, reply_role is not None, allow_push, True, reason_of(role), event_ts, others
        )
    except Exception as exc:
        _err("red flag decision", exc)
        return None


def _outcome_text(reason: str, *, sent: bool, suppressed: bool, muted: bool) -> str:
    if reason == "send":
        if sent and not suppressed:
            return "已送出"
        if muted:
            return "靜音中未送出"
        return "送出失敗"
    return {
        "deduped": "30 分鐘內已提醒過",
        "old_event": "訊息超過 10 分鐘未回",
        "in_care": "沒送（已在急診或送醫）",
        "future": "沒送（說的是之後的事）",
    }.get(reason, "未送出")


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
    """main 決定（並送出或不送）之後呼叫。

    回覆真的送出才記 30 分鐘去重（帶 message_id），這則提到、也被這次提醒涵蓋的家長都記；記失敗
    不影響私訊。私訊寫每位家長實際的結果，同一家長同一標籤 30 分鐘一次（先搶再送；DB 出錯時照送），
    在背景 daemon 執行緒送。"""
    try:
        if decision is None or decision.role not in PARENTS:
            return
        now_f = _now(now)
        key = str(message_id or "") or f"t{int(now_f)}"
        pairs = [(decision.role, label, decision.reason) for label in decision.labels]
        for entry in decision.others:
            if len(entry) >= 2 and entry[0] in PARENTS:
                pairs.append((entry[0], entry[1], entry[2] if len(entry) > 2 else decision.reason))
        if decision.reply and sent and not suppressed:
            for role in dict.fromkeys(role for role, _label, reason in pairs if reason == "send"):
                try:
                    parent_health_db.claim_alert(group_id, role, "reply", key, now=now_f)
                except Exception as exc:
                    _err("reply dedupe", exc)
        if not decision.dm:
            return
        claimed: dict[str, tuple[list[str], str]] = {}
        for role, label, reason in pairs:
            kind = f"dm:{label}"
            try:
                last = parent_health_db.last_alert(group_id, role, kind)
                if last is not None and now_f - last["sent_at"] < _DM_WINDOW_S:
                    continue
                if not parent_health_db.claim_alert(group_id, role, kind, key, now=now_f):
                    continue
            except Exception as exc:
                _err("dm dedupe", exc)  # 去重壞了寧可多送一次
            claimed.setdefault(role, ([], reason))[0].append(label)
        if not claimed:
            return
        said_at = _hhmm(decision.event_ts or now_f)
        order = [role for role in PARENTS if role in claimed]
        outcomes = {
            role: _outcome_text(claimed[role][1], sent=sent, suppressed=suppressed, muted=muted) for role in order
        }
        if len(set(outcomes.values())) == 1:
            who = "；".join(f"{role}提到{'、'.join(claimed[role][0])}" for role in order)
            text = f"🚨 LINE 群組：{who}（{said_at}）。群組提醒：{outcomes[order[0]]}"
        else:  # 兩位家長的結果不同，各寫各的
            each = "；".join(f"{role}提到{'、'.join(claimed[role][0])}，群組提醒：{outcomes[role]}" for role in order)
            text = f"🚨 LINE 群組（{said_at}）：{each}"
        _send_dm_background(text)
    except Exception as exc:
        _err("red flag outcome", exc)


# ── Discord 私訊 ────────────────────────────────────────────────────────────


def _send_dm(text: str) -> bool:
    """一律經 notify_discord.send_dm（用到時才查 module 屬性；conftest 只換掉它）。"""
    try:
        import notify_discord

        return bool(notify_discord.send_dm(text[:_DM_MAX_CHARS]))
    except Exception as exc:
        _err("discord dm", exc)
        return False


def _send_dm_background(text: str) -> None:
    """背景 daemon 執行緒送（send_dm 最多會卡 10 秒）；測試關掉背景時直接送，結果看得到。"""
    try:
        if not _BACKGROUND_ENABLED:
            _send_dm(text)
            return
        threading.Thread(target=_send_dm, args=(text,), daemon=True, name="parent-health-dm").start()
    except Exception as exc:
        _err("discord dm start", exc)


# ── 抽取 ────────────────────────────────────────────────────────────────────

_EXECUTOR: ThreadPoolExecutor | None = None  # 第一次用到才建（測試可以換成假的）
_INFLIGHT = threading.BoundedSemaphore(32)
_bg_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop: threading.Event | None = None


def _executor():
    global _EXECUTOR
    executor = _EXECUTOR
    if executor is not None:
        return executor
    with _bg_lock:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="parent-health")
        return _EXECUTOR


def observe_async(group_id: str, message_id: str, user_id: str | None, text: str, *, event_ts_ms) -> None:
    """每則文字訊息丟給背景抽取（不擋 webhook）。送出時就抓 DB 路徑；排隊滿 32 則就丟掉。"""
    try:
        if not _BACKGROUND_ENABLED:
            return
        if not group_id or not message_id or not isinstance(text, str) or not text.strip():
            return
        if _is_command_text(text):  # 指令（含「咪寶 /記住 …」）不抽取
            return
        db_path = parent_health_db._DB_PATH
        if not _INFLIGHT.acquire(blocking=False):
            return

        def run() -> None:
            try:
                observe_message(group_id, message_id, user_id, text, event_ts_ms=event_ts_ms, db_path=db_path)
            finally:
                _INFLIGHT.release()

        try:
            _executor().submit(run)
        except Exception:
            _INFLIGHT.release()
            raise
    except Exception as exc:
        _err("observe submit", exc)


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
    """抽取一則留言，只記記錄中、而且列上帳號仍是現在解析到的帳號的家長；回寫入筆數。"""
    try:
        if not group_id or not message_id or not isinstance(text, str) or not text.strip():
            return 0
        if _is_command_text(text):  # 指令（/健康、/記住、「咪寶 /記住 …」）整則略過
            return 0
        consents = parent_health_db.consent_states(group_id, db_path=db_path)
        default_on = _default_on(group_id)
        if not default_on and not any(row.get("status") == "on" for row in consents.values()):
            return 0
        roles = _resolve_roles()
        now_f = _now(now)
        event_ts = _event_ts(event_ts_ms, now_f)
        # 家族主群第一次看到這位家長就補預設 on 列；on_since 用這則留言的時間（背景比 webhook 晚，
        # 用現在的話這則和它的 119 判斷都會被 on_since 擋掉）
        if default_on and _ensure_default(group_id, roles, consents, now=min(now_f, event_ts), db_path=db_path):
            consents = parent_health_db.consent_states(group_id, db_path=db_path)
        states = _states(roles, consents)
        if _ON not in states.values():
            return 0
        names = _name_table(roles)
        extracted = _extract().extract(
            _with_parent_titles(text, names), _role_from(roles, user_id), other_names=_other_names(names)
        )
        findings = [finding for finding in extracted if states.get(getattr(finding, "role", None)) == _ON]
        if not findings:
            return 0
        return parent_health_db.add_events(
            group_id,
            findings,
            message_id=str(message_id),
            reporter_user_id=user_id or "",
            event_ts=event_ts,
            source="chat",
            now=now_f,
            db_path=db_path,
        )
    except Exception as exc:
        _err("observe", exc)
        return 0


def forget_message(group_id: str, message_id: str, *, db_path=None) -> int:
    """收回訊息：刪掉那則留言記下的健康紀錄，回刪除筆數。那則留言的背景抽取若還排在後面，
    再排一次同樣的刪除（同一個單執行緒 executor、冪等），抽取寫完之後也會被刪掉。"""
    message_id = str(message_id or "")
    try:
        deleted = parent_health_db.delete_message_events(group_id, message_id, db_path=db_path)
    except Exception as exc:
        _err("forget", exc)
        deleted = 0
    if _BACKGROUND_ENABLED and db_path is None and group_id and message_id:
        later_db = parent_health_db._DB_PATH

        def again() -> None:
            try:
                parent_health_db.delete_message_events(group_id, message_id, db_path=later_db)
            except Exception as exc:
                _err("forget queued", exc)

        try:
            _executor().submit(again)
        except Exception as exc:
            _err("forget submit", exc)
    return deleted


# ── 週報段落 ────────────────────────────────────────────────────────────────


def _weekly_line(s: _Summary) -> str:
    parts = []
    if s.taken_days:  # 只有忘了／拿藥時不寫「吃藥 0/7 天」
        parts.append(f"吃藥 {s.taken_days}/{s.denom} 天")
    if s.forgot_days:
        parts.append(f"忘了吃藥 {s.forgot_days} 天")
    parts += [f"{item} {days} 天" for item, days in s.exercise[:2]]
    parts += [f"{item} {days} 天" for item, days in s.sleep if item == _SLEEP_BAD]
    parts += [item for _day, item in s.visits_done[:2]]
    parts += [f"{item} {days} 天" for item, days in s.symptoms[:2]]
    parts += [f"⚠️ 提到{label}" for _day, label in s.flags[:1]]
    return f"{s.role}：{'、'.join(parts)}" if parts else ""


def weekly_section(group_id: str, now: float | None = None) -> str:
    """週日週報（家族熱話那則）後面接的段落：只有記錄中（沒暫停）的家長；沒有就回 ""。
    段落本身要原樣通過 outbound validator，不然回 ""（週報照舊）。"""
    try:
        ctx = _context(group_id, _now(now))
        on_roles = [role for role in PARENTS if ctx.states[role] == _ON]
        if not on_roles:
            return ""
        # 這週沒有紀錄的家長不列（Andrew 選的精簡）；兩位都沒有、也沒有下週看診就整段不出現。
        lines = [WEEKLY_MARKER] + [line for role in on_roles if (line := _weekly_line(_summary(ctx, role)))]
        next_week = [
            visit
            for visit in _safe_upcoming(ctx)
            if ctx.today < visit.day <= ctx.today + timedelta(days=7)
        ]
        if next_week:
            lines.append(
                "下週看診："
                + "、".join(
                    f"{_md_wd(v.day)}{v.clock + ' ' if v.clock else ''}{v.role} {v.keyword}" for v in next_week[:4]
                )
            )
        if len(lines) == 1:
            return ""
        section = "\n".join(lines)
        import output_validator

        result = output_validator.validate_outbound_text(section)
        if not result.ok or result.text != section:
            logger.error("parent health weekly section rejected by outbound validator")
            return ""
        return section
    except Exception as exc:
        _err("weekly section", exc)
        return ""


# ── 每日異常檢查（私訊 Andrew） ─────────────────────────────────────────────


@dataclass(frozen=True)
class _Anomaly:
    role: str
    kind: str
    text: str


def _anomaly_candidates(ctx: _Ctx, role: str) -> list[_Anomaly]:
    """這位家長在 ctx.now 這一刻每日檢查會列的項目（還沒扣 7 天抑制）。D＝ctx.today（台北）。
    - 2026-10-17：48 小時內提到的危象血壓（>180/120）、低血糖（<70），私訊寫當下怎麼做（parent_health_analysis）。
      2026-10-18 起每一筆讀數一個種類（`_urgent_kind`），最嚴重的在前；`_anomalies` 每種只留第一筆還沒提醒過的。
    - 用藥中斷：開始記錄 ≤ D-17 的 00:00、D-17..D-4 有 ≥ 5 天說吃藥、D-3..D（到現在）都沒有。
    - 忘了吃藥：過去 24 小時有說。
    - 同一症狀 D-6..D ≥ 3 天（緊急字詞不算症狀）；睡不好 D-6..D ≥ 4 天。
    只算這次開始記錄以來（event_ts ≥ on_since）的紀錄：暫停又恢復時，之前那段不會一恢復就觸發。"""
    today = ctx.today
    on_since = ctx.consents[role]["on_since"]
    rows = [
        row
        for row in _events(ctx, role, today - timedelta(days=17), today)
        if on_since <= row["event_ts"] <= ctx.now
    ]
    med = [row for row in rows if row["category"] == "med"]
    candidates: list[_Anomaly] = []
    try:  # 新模組壞了照送下面原本的幾條
        import parent_health_analysis as analysis

        for kind, day, value, event_ts in analysis.urgent_readings(rows, since_ts=ctx.now - _URGENT_LOOKBACK_S):
            candidates.append(
                _Anomaly(role, _urgent_kind(kind, day, value), analysis.urgent_dm_text(role, _PRONOUN[role], kind, value, event_ts))
            )
    except Exception as exc:
        _err("urgent readings", exc)
    if on_since <= _day_start_ts(today - timedelta(days=17)):
        baseline = {
            row["day"] for row in med
            if row["item"] == _MED_TAKEN and today - timedelta(days=17) <= row["day"] <= today - timedelta(days=4)
        }
        gap = {row["day"] for row in med if row["item"] == _MED_TAKEN and row["day"] >= today - timedelta(days=3)}
        if len(baseline) >= 5 and not gap:
            candidates.append(_Anomaly(role, "med_gap", f"{role} 3 天沒提到吃藥（前兩週有 {len(baseline)} 天會說）"))
    forgot = [row for row in med if row["item"] == _MED_FORGOT and row["event_ts"] >= ctx.now - 86400]
    if forgot:
        latest = max(forgot, key=lambda row: (row["event_ts"], row["event_id"]))
        candidates.append(_Anomaly(role, "med_forgot", f"{role}說忘了吃藥（{_md(latest['day'])}）"))
    week = [row for row in rows if row["day"] >= today - timedelta(days=6)]
    symptoms = _ranked_by_days(
        [
            row
            for row in week
            if row["category"] == "body" and not row["value"] and row["flag"] != parent_health_db.REDFLAG
        ]
    )
    for item, days in symptoms:
        if days >= 3:
            candidates.append(_Anomaly(role, f"symptom:{item}", f"{role} 7 天內有 {days} 天提到{item}"))
    bad_sleep = len(_distinct_days(week, _SLEEP_BAD, "sleep"))
    if bad_sleep >= 4:
        candidates.append(_Anomaly(role, "sleep_bad", f"{role} 7 天內有 {bad_sleep} 天睡不好"))
    return candidates


def _urgent_kind(kind: str, day: date, value: str) -> str:
    """危象血壓、低血糖的去重種類：一筆讀數（讀數那天＋數字）一個，所以同一筆只會提醒一次，新的一筆照樣提醒。"""
    return f"{kind}:{day.isoformat()}:{value}"


def _anomalies(ctx: _Ctx) -> list[_Anomaly]:
    """只看記錄中（沒暫停）的家長；同一項 7 天內私訊過就不列。規則見 _anomaly_candidates。
    危象血壓、低血糖（Andrew 10/18：「同一筆讀數只提醒一次，每天最多一則」）：每一筆讀數是自己的種類，
    提醒過就不再列；每位家長每種只留第一筆還沒提醒過的（候選已照嚴重程度排好），沒輪到的隔天還在 48 小時內就再排。
    私訊只有角色、類別、標籤、天數；2026-10-17 起危象血壓、低血糖另帶讀數與留言時間。"""
    found: list[_Anomaly] = []
    for role in PARENTS:
        if ctx.states[role] != _ON:
            continue
        taken: set[str] = set()
        for anomaly in _anomaly_candidates(ctx, role):
            base = anomaly.kind.split(":", 1)[0]
            if base in _URGENT_KINDS and base in taken:
                continue
            last = parent_health_db.last_alert(ctx.group_id, role, anomaly.kind, db_path=ctx.db_path)
            if last is not None and ctx.now - last["sent_at"] < _ALERT_SUPPRESS_S:
                continue
            if base in _URGENT_KINDS:
                taken.add(base)
            found.append(anomaly)
    return found


def anomaly_lines(group_id: str, now: float | None = None) -> list[str]:
    """現在會私訊的異常（已扣掉 7 天內私訊過的）；只讀。2026-10-17 起可能帶讀數：每日維護不要呼叫或轉述。"""
    try:
        return [anomaly.text for anomaly in _anomalies(_context(group_id, _now(now)))]
    except Exception as exc:
        _err("anomaly lines", exc)
        return []


def run_daily_check(now: float | None = None) -> int:
    """每天台北 21:00 之後一次：每個有記錄中家長的群組先搶 daily_check:<日期>（重啟、雙程序都不重送），
    每項異常也先搶再送；send_dm 結果不明也不重送。回送出的私訊數。"""
    try:
        now_f = _now(now)
        local = _local(now_f)
        if local.hour < _DAILY_HOUR:
            return 0
        day = local.date()
        sent = 0
        for group_id in parent_health_db.groups_with_consent():
            try:
                if not parent_health_db.claim_alert(group_id, "", "daily_check", day.isoformat(), now=now_f):
                    continue
                lines = [
                    anomaly.text
                    for anomaly in _anomalies(_context(group_id, now_f))
                    if parent_health_db.claim_alert(group_id, anomaly.role, anomaly.kind, day.isoformat(), now=now_f)
                ]
                if not lines:
                    continue
                text = f"🩺 爸媽健康每日檢查（{_md(day)}）"
                for line in lines:
                    if len(text) + 1 + len(line) > _DM_MAX_CHARS:
                        break
                    text += "\n" + line
                if _send_dm(text):
                    sent += 1
            except Exception as exc:
                _err("daily check group", exc)
        return sent
    except Exception as exc:
        _err("daily check", exc)
        return 0


# ── 週日分析私訊（2026-10-17） ─────────────────────────────────────────────

# 每日檢查的種類 → 週私訊「這週每日檢查的項目」的固定說法（症狀用詞表標籤本身）
_DAILY_KIND_TEXT = {
    "bp_crisis": "血壓很高",
    "glucose_low": "血糖偏低",
    "med_gap": "3 天沒提到吃藥",
    "med_forgot": "忘了吃藥",
    "sleep_bad": "睡不好",
}


def _week_daily_items(ctx: _Ctx, role: str, week_start: date) -> list[str]:
    """這 7 天每天 21:00 的每日檢查會列的項目：用現在還在的紀錄重算，不讀去重表（刪掉的錯記、暫停前的
    紀錄都不會回來）。只有固定說法與詞表標籤。"""
    found: list[str] = []
    for offset in range((ctx.today - week_start).days + 1):
        day = week_start + timedelta(days=offset)
        moment = min(ctx.now, _day_start_ts(day) + _DAILY_HOUR * 3600)
        for anomaly in _anomaly_candidates(replace(ctx, now=moment, today=day), role):
            base = anomaly.kind.split(":", 1)[0]  # 危象、低血糖的種類帶著讀數（_urgent_kind）
            text = _DAILY_KIND_TEXT.get(base)
            if text is None and anomaly.kind.startswith("symptom:"):
                text = anomaly.kind[len("symptom:"):]
            if text and text not in found:
                found.append(text)
    return found


def _weekly_analysis_text(ctx: _Ctx) -> str | None:
    """週日私訊 Andrew 的完整版（這 7 天）：記錄中的家長各一段；兩位這週都沒有紀錄就回 None（不送）。"""
    import parent_health_analysis as analysis

    today = ctx.today
    week_start = today - timedelta(days=6)
    visits = _safe_upcoming(ctx)
    lines: list[str] = []
    any_records = False
    for role in PARENTS:
        if ctx.states[role] != _ON:
            continue
        on_since = ctx.consents[role]["on_since"]
        on_day = _local(on_since).date()
        rows = _analysis_rows(ctx, role)
        record_days = len({row["day"] for row in rows if row["day"] >= week_start})
        any_records = any_records or bool(record_days)
        daily_items = _week_daily_items(ctx, role, week_start)
        upcoming = [v for v in visits if v.role == role and v.day <= today + timedelta(days=14)]
        if record_days:
            denom = max(1, min(7, (today - on_day).days + 1))
            started = f"{_md(on_day)} 開始記錄，" if on_day > week_start else ""
            lines.append(f"{role}（{started}{denom} 天有 {record_days} 天有紀錄）")
            prev_ok = on_since <= _day_start_ts(today - timedelta(days=13))
            findings = analysis.analyze(
                rows, today=today, visits=_role_visits(visits, role), week=True, prev_week_ok=prev_ok,
                pronoun=_PRONOUN[role],
            )
            lines += [f"• {finding.dm}" for finding in findings]
            lines.append(f"• {analysis.readings_summary(rows, today=today, prev_week_ok=prev_ok)}")
        else:
            lines.append(f"{role}：這週沒有紀錄")
        if daily_items:
            lines.append(f"• 這週有符合每日檢查條件的：{'、'.join(daily_items)}")
        if upcoming:
            lines.append("• 下次看診：" + "、".join(visit.when() for visit in upcoming[:2]))
    if not any_records:
        return None
    footer = "依公開衛教整理，不是診斷"
    cut = "（太長，後面省略）"
    text = f"🩺 爸媽健康週分析（{_md(week_start)}–{_md(today)}）"
    for line in lines:  # 太長就截斷並註明；最後的免責句一定留著
        if len(text) + len(line) + len(cut) + len(footer) + 3 > _DM_MAX_CHARS:
            text += "\n" + cut
            break
        text += "\n" + line
    return text + "\n" + footer


def _run_weekly_analysis(now: float | None = None) -> int:
    """台北週日 21:00 之後一次：每個有記錄中家長的群組先組好文字，有內容才搶 weekly_analysis:<日期>
    再送（重啟、雙程序都不重送；send 結果不明也不重送）。和每日檢查分開搶、分開出錯。回送出的私訊數。"""
    try:
        now_f = _now(now)
        local = _local(now_f)
        if local.weekday() != 6 or local.hour < _DAILY_HOUR:
            return 0
        key = local.date().isoformat()
        sent = 0
        for group_id in parent_health_db.groups_with_consent():
            try:
                last = parent_health_db.last_alert(group_id, "", "weekly_analysis")
                if last is not None and last["key"] == key:
                    continue
                text = _weekly_analysis_text(_context(group_id, now_f))
                if not text:
                    continue
                if not parent_health_db.claim_alert(group_id, "", "weekly_analysis", key, now=now_f):
                    continue
                if _send_dm(text):
                    sent += 1
            except Exception as exc:
                _err("weekly analysis group", exc)
        return sent
    except Exception as exc:
        _err("weekly analysis", exc)
        return 0


def _daily_loop(stop: threading.Event) -> None:
    if stop.wait(_STARTUP_DELAY_S):
        return
    while not stop.is_set():
        for job in (run_daily_check, _run_weekly_analysis):  # 各自搶、各自出錯
            try:
                job()
            except Exception as exc:  # 兩個都不丟例外；這裡再接一次，執行緒不會無聲停掉
                _err("daily loop", exc)
        if stop.wait(_CHECK_INTERVAL_S):
            return


def start_background() -> None:
    """lifespan 啟動時呼叫：每 10 分鐘醒來看要不要做每日檢查。執行緒還活著就不再起。"""
    global _thread, _stop
    try:
        if not _BACKGROUND_ENABLED:
            return
        with _bg_lock:
            if _thread is not None and _thread.is_alive():
                return
            _stop = threading.Event()
            _thread = threading.Thread(target=_daily_loop, args=(_stop,), daemon=True, name="parent-health-daily")
            _thread.start()
    except Exception as exc:
        _err("background start", exc)


def stop_background(timeout: float = 2.0) -> None:
    """lifespan 結束時呼叫：停每日執行緒；抽取的 executor 不再收新工作（排隊中的照樣做完）。"""
    global _EXECUTOR
    try:
        with _bg_lock:
            thread, stop, executor = _thread, _stop, _EXECUTOR
            _EXECUTOR = None
        if stop is not None:
            stop.set()
        if thread is not None:
            thread.join(timeout=timeout)
        shutdown = getattr(executor, "shutdown", None)
        if shutdown is not None:
            shutdown(wait=False)
    except Exception as exc:
        _err("background stop", exc)


def status_summary(now: float | None = None) -> dict:
    """給每日維護讀（另一個程序，看不到這裡的執行緒）：狀態與計數，不含任何紀錄內容或 ID。

    daily_check_overdue 看 DB：有人同意、而且該做的那次每日檢查（21:15 之後算今天，之前算昨天）
    沒有搶到紀錄 → 每日檢查的執行緒可能沒在跑。"""
    try:
        now_f = _now(now)
        info = {"status": "ok"}
        info.update(parent_health_db.stats(now=now_f))
        local = _local(now_f)
        due_day = local.date() if (local.hour, local.minute) >= (_DAILY_HOUR, 15) else local.date() - timedelta(days=1)
        due_ts = _day_start_ts(due_day) + _DAILY_HOUR * 3600
        oldest = info.pop("oldest_on_since", None)
        info["daily_check_overdue"] = bool(
            info.get("groups")
            and oldest is not None
            and int(oldest) < due_ts
            and (info.get("last_daily_check") or "") < due_day.isoformat()
        )
        return info
    except Exception as exc:
        return {"status": "error", "error_type": type(exc).__name__}
