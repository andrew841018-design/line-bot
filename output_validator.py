"""Local outbound factual-safety checks before text reaches LINE.

This module is intentionally deterministic and network-free.  It is the last
gate before delivery, not a replacement for source-aware answer generation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from zoneinfo import ZoneInfo

import reply_policy


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

_YOUTUBE_LINK_FAILURE_SAFE_TEXT = ""

_LOW_VALUE_REPLY_SAFE_TEXT = ""

_INTERNAL_TRACE_SAFE_TEXT = ""

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

_YOUTUBE_CONTEXT_RE = re.compile(
    r"(?:youtube|youtu\.be|shorts|直播|影片|連結|網址|網頁)", re.IGNORECASE,
)
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

_LINK_NONANSWER_RE = re.compile(
    r"(?:連結|網址|網頁|影片|內容)[^。！？!?\n]{0,18}(?:讀不到|抓不到|沒有取得|沒有.*具體內容)|"
    r"(?:無法|不能|沒辦法)[^。！？!?\n]{0,30}(?:根據|判斷|確認)[^。！？!?\n]{0,35}(?:連結|影片|主張|問題)|"
    r"(?:只有|僅有|僅包含|只能提供)[^。！？!?\n]{0,40}(?:一般資訊|網域資訊|平台資訊)|"
    r"(?:請|可嘗試|(?:如果|若)(?:您|你)?方便)[^。！？!?\n]{0,18}(?:提供|補充|補貼)[^。！？!?\n]{0,24}(?:標題|描述|截圖|關鍵字)|"
    r"(?:只取得|只拿到)[^。！？!?\n]{0,20}(?:metadata|標題|描述)|"
    r"(?:未取得|沒有取得|讀不到|抓不到)[^。！？!?\n]{0,15}(?:字幕|逐字稿|內容)|"
    r"連結解析流程沒有正確啟動|請重新貼一次連結|我會盡力協助查找|"
    r"(?:我會|我可以|我能)[^。！？!?\n]{0,45}(?:重新抓取|協助查找)|"
    r"^(?:直播|影片)內容隨時變動$|^具體涵蓋的主題、發布者及時效性$|"
    r"^若(?:您|你)是想查詢特定主題的(?:直播|影片)內容$|"
    r"^(?:請)?(?:稍後|晚點)(?:再試|重試)|^才能判斷$|"
    r"(?:這|此|該)[^。！？!?\n]{0,10}(?:是|為)[^。！？!?\n]{0,30}(?:youtube|shorts|短影音)[^。！？!?\n]{0,12}(?:連結|網址)",
    re.IGNORECASE,
)
# 2026-09-26「由於沒有取得影片的字幕…，無法判斷其具體論述…」: once the reply
# itself says the subtitles are missing, a subject-less "cannot judge" clause
# is the bot describing its missing material, not an evaluation.
_MISSING_TRANSCRIPT_RE = re.compile(
    r"(?:沒有|沒|未能?|無法)(?:取得|拿到|抓到|讀到|看到)?[^。！？!?\n，,]{0,12}(?:字幕|逐字稿)"
    r"|只(?:看得到|看到|拿到|取得)[^。！？!?\n，,]{0,8}(?:標題|描述)"
)
# The whole clause is only that status (「由於沒有取得影片的字幕或逐字稿」);
# 「這支影片沒有字幕會讓聽障者難以理解口白」 says something more.
_MISSING_TRANSCRIPT_STATUS_RE = re.compile(
    r"(?:由於|因為|因)?(?:目前|暫時)?(?:這支|這個|這則|該|此)?(?:影片|短片|連結|直播)?(?:目前|暫時)?"
    r"(?:沒有|沒|未能?|無法)(?:取得|拿到|抓到|讀到|看到)?(?:這支|該)?(?:影片)?的?"
    r"(?:字幕|逐字稿)(?:或(?:字幕|逐字稿))?(?:內容)?"
    r"|(?:目前)?只(?:看得到|看到|拿到|取得)(?:這支|該)?(?:影片)?的?(?:標題|描述)"
    r"(?:(?:和|與|、|及)(?:頻道|描述|標題))?(?:資訊|資料)?"
)
_SELF_CANNOT_ASSESS_RE = re.compile(
    r"^(?:由於|因此|所以|因而|也|進而)?(?:我|咪寶)?(?:暫時|目前)?(?:無法|不能|沒辦法)(?:進一步)?"
    r"(?:判斷|核實|確認|查證|證實|評估)"
)
# Next to that status, advice (「…所以建議先核對原始公告」) or a stated reason the
# claim is wrong (「無法判斷真假因為標題數字與官方統計不符」) is still an answer —
# a keyword inside what could not be judged (「無法判斷影片中的數據…」) is not,
# nor is asking the user to supply, repost or open the link themselves.
_FRAGMENT_ADVICE_RE = re.compile(
    r"建議|應該|應先|最好|記得|避免|小心|要注意|可以先|請先|先(?:比對|核對|查證|確認)"
)
_FRAGMENT_REASON_RE = re.compile(
    r"(?:因為|由於|原因是)[^。！？!?；;，,\n]*(?:不符|錯|誤|過時|不實|假)"
)
_FRAGMENT_DEFLECTION_RE = re.compile(
    r"提供|補充|補貼|重貼|重新貼|稍後|晚點|點擊|點開|自己點|自行點|自己看|自行觀看"
)


def _explained_after_colon(fragment: str) -> bool:
    """「無法判斷完整論述是否正確：標題把年利率誤寫成月利率」 gives the evidence."""
    parts = re.split(r"[：:]", fragment, maxsplit=1)
    if len(parts) < 2:
        return False
    rest = parts[1].strip()
    return (
        len(re.sub(r"[^\w]", "", rest)) >= 6
        and not _MISSING_TRANSCRIPT_STATUS_RE.fullmatch(rest)
        and not _SELF_CANNOT_ASSESS_RE.search(rest)
        and not _FRAGMENT_DEFLECTION_RE.search(rest)
    )
_LINK_TROUBLESHOOT_RE = re.compile(
    r"(?:設為私人|獲邀帳號|權限設定|地區限制|DNS|HTTP\s*[45]\d\d|"
    r"清除[^。！？!?\n]{0,8}快取|更新[^。！？!?\n]{0,8}瀏覽器|"
    r"(?:設定|新增|建立)[^。！？!?\n]{0,20}提醒|提醒時間)", re.IGNORECASE,
)


def is_link_failure_nonanswer(text: str) -> bool:
    """Reject status/deflection-only replies, retaining useful answer clauses."""
    if not _YOUTUBE_CONTEXT_RE.search(text or ""):
        return False
    # Echoed URLs or a source footer cannot rescue an otherwise empty answer.
    body = re.sub(r"https?://[^\s，。]+", "", text, flags=re.IGNORECASE)
    fragments = re.split(r"[。！？!?；;，,\n]+|但是?|不過|然而", body)
    missing_transcript = bool(_MISSING_TRANSCRIPT_RE.search(body))
    rejected = False
    for fragment in fragments:
        fragment = fragment.strip(" \t*#-，,:：")
        if not fragment or re.fullmatch(
            r"(?:來源|出處)(?:[:：]\s*(?:YouTube(?:\s*Shorts)?))?|"
            r"抱歉|很抱歉|不好意思|謝謝您的理解|因此|所以|如果方便|希望這有幫助", fragment,
            re.IGNORECASE,
        ):
            continue
        if _LINK_TROUBLESHOOT_RE.search(fragment):
            return False
        if missing_transcript:
            if _MISSING_TRANSCRIPT_STATUS_RE.fullmatch(fragment):
                rejected = True
                continue
            advice = bool(_FRAGMENT_ADVICE_RE.search(fragment)) and not _FRAGMENT_DEFLECTION_RE.search(fragment)
            if _SELF_CANNOT_ASSESS_RE.search(fragment):
                if advice or _FRAGMENT_REASON_RE.search(fragment) or _explained_after_colon(fragment):
                    return False
                rejected = True
                continue
            if advice and not _YOUTUBE_LINK_FAILURE_RE.search(fragment):
                return False
        if not (_YOUTUBE_LINK_FAILURE_RE.search(fragment) or _LINK_NONANSWER_RE.search(fragment)):
            return False
        rejected = True
    return rejected

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
_INTERNAL_TRACE_RE = re.compile(
    r"(?:"
    r"^\s*(?:\[\[?\s*)?(?:思考|推理|內心獨白|草稿|系統思考)(?:\s*\]?\])?\s*(?:[:：]|$)"
    r"|^\s*\[\[?\s*分析\s*\]?\]\s*[:：]?"
    r"|^\s*(?:reasoning|thought|analysis)\s*(?:[:：]|$)"
    r"|^\s*<\s*(?:thinking|analysis|reasoning)\s*>"
    r"|^\s*```(?:thinking|analysis|reasoning)"
    r"|使用者貼了一個|判斷問題類型|處理連結內容|遵循規則|執行 concise_search|^\s*回覆結構\s*[:：]"
    r"|^\s*(?:思緒|內部判斷|判斷結果)\s*[:：]"
    r"|^\s*[（(]?(?:保持沉默|靜默)[）)。.!！\s]*$|(?:不產生(?:實際)?回覆|這則訊息不需要回覆)|(?:我|咪寶|助手|助理|bot)(?:應該|應|會|決定|選擇)?(?:保持沉默|靜默|不回覆)|(?:因此|所以)[，,\s]*(?:應該|應)?保持沉默|^\s*(?:決定|選擇)(?:不回覆|不回應)|(?:這|此)[^。！？!?\n]{0,24}(?:訊息|閒聊)[^。！？!?\n]{0,12}(?:不用|不必|無需|不需)(?:回覆|回應)"
    r"|The user (?:posted|shared|sent)|I need to determine|response structure"
    r")",
    re.IGNORECASE | re.MULTILINE,
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
    return is_link_failure_nonanswer(_fold_width_digits(text))


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


def _internal_trace_reply(text: str) -> bool:
    head = (text or "").lstrip()[:2500]
    if not head:
        return False
    if _INTERNAL_TRACE_RE.search(head):
        return True
    strong_markers = (
        "我需要判斷使用者",
        "I need to determine",
        "The user posted",
        "The user shared",
        "The user sent",
    )
    if any(marker.lower() in head.lower() for marker in strong_markers):
        return True
    markers = (
        "我應該先",
        "我會嘗試",
        "如果搜尋",
        "第一句判斷句",
        "規則 6",
        "Google 搜尋該連結",
        "concise_search",
    )
    return sum(1 for marker in markers if marker in head) >= 2


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

    # A printed placeholder such as 「（輸出空字串）」 is never a reply (2026-10-04).
    if reply_policy.is_printed_placeholder(original):
        return ValidationResult(ok=False, text="", reason="empty_output_marker")

    runtime = _runtime_now(now)
    if _internal_trace_reply(original):
        return ValidationResult(
            ok=False,
            text=_INTERNAL_TRACE_SAFE_TEXT,
            reason="internal_trace_leak",
        )

    if _youtube_link_failure_without_context(original):
        return ValidationResult(ok=False, text="", reason="youtube_link_failure")

    reason = _stale_current_year_reason(original, now=runtime)
    if reason:
        return ValidationResult(ok=False, text=_STALE_TIME_SAFE_TEXT, reason=reason)

    if _INTERNAL_SOURCE_RE.search(_fold_width_digits(original)):
        return ValidationResult(
            ok=False,
            text=_INTERNAL_SOURCE_SAFE_TEXT,
            reason="internal_source_claim",
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
