"""Local outbound factual-safety checks before text reaches LINE.

This module is intentionally deterministic and network-free.  It is the last
gate before delivery, not a replacement for source-aware answer generation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from zoneinfo import ZoneInfo


_TW = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    text: str
    reason: str = ""


_STALE_TIME_SAFE_TEXT = (
    "這則回覆在送出前被擋下：它使用了過期的年份基準。"
    "請重新問一次，我會以目前台灣時間重新查證。"
)

_INTERNAL_SOURCE_SAFE_TEXT = (
    "這則回覆在送出前被擋下：它把模型知識截止或內部資料庫當成來源。"
    "請重新問一次，我會改用可查證來源回答。"
)

_UNVERIFIED_CURRENT_DATA_SAFE_TEXT = (
    "這則回覆在送出前被擋下：它宣稱最新或官方統計，但缺少來源或資料日期。"
    "請重新問一次，我會改用官方來源查證後回答。"
)

_YOUTUBE_LINK_FAILURE_SAFE_TEXT = (
    "這則回覆在送出前被擋下：YouTube 連結解析流程沒有正確啟動。"
    "請重新貼一次連結，我會改用 yt-dlp、oEmbed、HTML metadata 重新抓取。"
)

_LOW_VALUE_REPLY_SAFE_TEXT = ""

_CURRENT_YEAR_CLAIM_PATTERNS = (
    re.compile(
        r"(?:現在|目前|當前|今天)[，,、\s]*(?:日期|時間|年份)?[，,、\s]*(?:是|為|=)\s*(20\d{2})\s*年?"
    ),
    re.compile(
        r"(?:現在|目前|當前|今天)[^\n。！？!?]{0,20}(20\d{2})[-/年][0-1]?\d(?:[-/月][0-3]?\d日?)?"
    ),
    re.compile(r"(?:現在|目前|當前)[^\n。！？!?]{0,10}已經是\s*(20\d{2})\s*年?"),
    re.compile(r"今年[^\n。！？!?]{0,8}(?:是|為|=)?\s*(20\d{2})\s*年?"),
    re.compile(r"\b(?:current year|today is|it is)\s*(20\d{2})\b", re.IGNORECASE),
)

_INTERNAL_SOURCE_RE = re.compile(
    r"(?:"
    r"(?:我的|我|咪寶|模型|系統|內部|本機)(?:資料庫|知識|訓練資料|資料集)[^\n。！？!?]{0,24}"
    r"(?:只到|截至|截止|更新到|停留在|最新到)"
    r"|(?:模型|系統|我的|咪寶)[^\n。！？!?]{0,8}知識截止"
    r"|knowledge\s+cutoff|training\s+data"
    r")",
    re.IGNORECASE,
)

_HIGH_RISK_CURRENT_DATA_RE = re.compile(
    r"(?:最新(?:公布|發布|資料|數據|統計)|官方(?:統計|數據|資料)|完整數據|完整資料|統計資料|國家移民管理局|移民管理局)"
)

_NUMERIC_FACT_RE = re.compile(
    r"(?:20\d{2}|[0-9０-９][0-9０-９,，.％%萬億千百百萬年月日人件筆元美元台幣新台幣]*|[一二三四五六七八九十百千萬億]+(?:人|件|筆|元|美元|台幣|新台幣|％|%))"
)

_VERIFICATION_HINT_RE = re.compile(
    r"(?:"
    r"https?://|www\.|"
    r"(?:資料|統計|數據)?(?:發布|公布)日期[:：]|"
    r"(?:資料|統計|數據)?截至[:：]?\s*20\d{2}|"
    r"資料日期[:：]\s*20\d{2}|"
    r"retrieved\s+20\d{2}|accessed\s+20\d{2}"
    r")",
    re.IGNORECASE,
)

_YOUTUBE_CONTEXT_RE = re.compile(r"(?:youtube|youtu\.be|直播|影片連結|影片網址)", re.IGNORECASE)
_YOUTUBE_LINK_FAILURE_RE = re.compile(
    r"(?:"
    r"直接點擊|點擊觀看|自己點|自行點|請點擊連結|請打開原連結|請到\s*YouTube\s*查看|"
    r"點不開|打不開|看不了|連結壞了|"
    r"未提供預覽內容|"
    r"需透過連結本身才能得知|"
    r"無法解析(?:此)?直播|"
    r"無法(?:讀取|取得)[^\n。！？!?]{0,12}(?:YouTube|影片|直播|內容)|"
    r"(?:僅包含|只有|只能提供)\s*YouTube\s*的?一般網域資訊|"
    r"只能提供一般資訊|"
    r"無法像人類一樣觀看|"
    r"不能判斷這個直播|"
    r"提供更多關鍵字|請提供影片標題"
    r")",
    re.IGNORECASE,
)

_CHECKIN_COMMITMENT_RE = re.compile(
    r"(?:我|我們|大家)\s*"
    r"(?:一定會|會)?\s*"
    r"(?:好好|記得|準時|確實|乖乖|按時|去)?\s*"
    r"(?:打卡|簽到|報到)"
)
_ACK_PREFIX_RE = re.compile(
    r"^\s*(?:好(?:喔|哦|啊|啦)?|好的|收到|了解|知道了|ok|OK|沒問題|放心)"
    r"[，,、。！!\s]*"
)
_USEFUL_CHECKIN_CONTEXT_RE = re.compile(
    r"(?:已設定|設定提醒|⏰\s*提醒|提醒[（(:：]|將於|預計|建議|地點|入口|期限|截至|"
    r"資料|規定|規則|需要|前完成|後補|時間[:：]|日期[:：]|20\d{2}[-/年])"
)
_LOW_VALUE_HELPLESS_RE = re.compile(
    r"(?:"
    r"有一定道理(?:，|,)?但需要進一步查證|"
    r"需要進一步查證(?:和|與)?具體分析|"
    r"需要(?:多方面|更多資訊|更完整資訊|綜合)考量|"
    r"需要更多資訊才能判斷|"
    r"保持健康的生活方式|"
    r"均衡飲食[、，,](?:並)?適度運動|"
    r"尋求專業(?:醫師|人士|協助)|"
    r"諮詢專業(?:醫師|人士|意見)|"
    r"視情況而定|"
    r"因人而異"
    r")"
)
_CONCRETE_HELP_RE = re.compile(
    r"(?:"
    r"https?://|www\.|(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:/\S*)?|"
    r"20\d{2}|[0-9０-９]{1,3}(?:[:：][0-9０-９]{2}|[/.-][0-9０-９]|[%％]|"
    r"\s*(?:元|萬|億|人|件|筆|次|天|小時|分鐘|公里|km|KM))|"
    r"第[一二三四五六七八九十百]+[點項]|"
    r"(?:來源|出處|依據|資料日期|截至|發布日期)[:：]|"
    r"(?:已設定|設定提醒|提醒[:：]|預約編號|驗證碼|接送網址)|"
    r"(?:第一步|第二步|第三步|步驟|先.+再|先.+最後|做法[:：]|處理方式[:：])|"
    r"(?:打給|聯絡|申請|取消|改期|上傳|截圖|保存|不要入金|停止交易)"
    r")",
    re.IGNORECASE,
)


def _fold_width_digits(text: str) -> str:
    return text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))


def _runtime_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(tz=_TW)
    if now.tzinfo is None:
        return now.replace(tzinfo=_TW)
    return now.astimezone(_TW)


def _stale_current_year_reason(text: str, *, now: datetime) -> str:
    current_year = now.year
    folded = _fold_width_digits(text)
    for pattern in _CURRENT_YEAR_CLAIM_PATTERNS:
        for match in pattern.finditer(folded):
            try:
                claimed_year = int(match.group(1))
            except (TypeError, ValueError):
                continue
            if claimed_year != current_year:
                return f"stale_current_year:{claimed_year}!={current_year}"
    return ""


def _high_risk_current_data_without_verification(text: str) -> bool:
    folded = _fold_width_digits(text)
    return (
        bool(_HIGH_RISK_CURRENT_DATA_RE.search(folded))
        and bool(_NUMERIC_FACT_RE.search(folded))
        and not bool(_VERIFICATION_HINT_RE.search(folded))
    )


def _youtube_link_failure_without_context(text: str) -> bool:
    folded = _fold_width_digits(text)
    return bool(_YOUTUBE_CONTEXT_RE.search(folded)) and bool(
        _YOUTUBE_LINK_FAILURE_RE.search(folded)
    )


def _low_value_checkin_commitment_reply(text: str) -> bool:
    folded = _fold_width_digits(text)
    compact = re.sub(r"\s+", "", folded)
    if not compact:
        return False
    if _USEFUL_CHECKIN_CONTEXT_RE.search(folded):
        return False
    if not _CHECKIN_COMMITMENT_RE.search(folded):
        return False
    return bool(_ACK_PREFIX_RE.match(folded)) or len(compact) <= 36


def _low_value_helpless_reply(text: str) -> bool:
    folded = _fold_width_digits(text)
    compact = re.sub(r"\s+", "", folded)
    if not compact:
        return False
    if not _LOW_VALUE_HELPLESS_RE.search(folded):
        return False
    if _CONCRETE_HELP_RE.search(folded):
        return False
    return len(compact) <= 220


def validate_outbound_text(text: str, *, now: datetime | None = None) -> ValidationResult:
    """Validate a LINE-bound text message.

    The validator blocks only patterns that are highly likely to be harmful or
    noisy: stale runtime-year assertions, model/database cutoff claims, latest
    or official-statistics claims without a verifiable source hint, YouTube
    deflection text, short bot-as-human check-in commitments, and generic
    filler that does not add concrete help.  It does not attempt to prove every
    fact in arbitrary prose.
    """
    original = text or ""
    if not original.strip():
        return ValidationResult(ok=True, text=original)

    runtime = _runtime_now(now)
    reason = _stale_current_year_reason(original, now=runtime)
    if reason:
        return ValidationResult(ok=False, text=_STALE_TIME_SAFE_TEXT, reason=reason)

    if _INTERNAL_SOURCE_RE.search(_fold_width_digits(original)):
        return ValidationResult(
            ok=False,
            text=_INTERNAL_SOURCE_SAFE_TEXT,
            reason="internal_source_claim",
        )

    if _youtube_link_failure_without_context(original):
        return ValidationResult(
            ok=False,
            text=_YOUTUBE_LINK_FAILURE_SAFE_TEXT,
            reason="youtube_link_failure",
        )

    if _low_value_checkin_commitment_reply(original):
        return ValidationResult(
            ok=False,
            text=_LOW_VALUE_REPLY_SAFE_TEXT,
            reason="low_value_checkin_commitment",
        )

    if _low_value_helpless_reply(original):
        return ValidationResult(
            ok=False,
            text=_LOW_VALUE_REPLY_SAFE_TEXT,
            reason="low_value_helpless_reply",
        )

    if _high_risk_current_data_without_verification(original):
        return ValidationResult(
            ok=False,
            text=_UNVERIFIED_CURRENT_DATA_SAFE_TEXT,
            reason="unverified_current_or_official_data",
        )

    return ValidationResult(ok=True, text=original)
