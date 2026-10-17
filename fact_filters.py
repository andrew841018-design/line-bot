"""fact_filters — 哪些「事實」不能存進家人記憶（2026-10-11 每日記憶）。

即時抽取（main._extract_facts_now）和每日 job（jobs/daily_memory.py）共用，不 import main。
reject_reason(fact) 回 "" 代表可以存；不能存就回類別名，不回命中的字（log 也只記類別）。

依序檢查：
1. private：PRIVATE_FACT_RE（2026-10-09 起的私人細節，從 main 原樣搬來）。
2. contact：地址、電話（和回覆的聯絡資料關卡同一套判斷，含中文數字地址）、email。
   事實是那道關卡的「已知來源」，這一類永遠不能放寬。
3. strict:<類別>：10/11 重建時另外擋的窄類別；每類一條，要放寬某類就刪那一行。
4. meta：寫給 bot 的話、程式的標記、連結與帳號、太長的句子——每條事實都進提示，不能變成指令。
5. kinship：通用稱謂（從說話的人的角度，對不到群組裡的人）。
6. name：本機名單裡的名字（別名、角色、短名、顯示名稱、隱私詞）；每次呼叫都重讀。
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Iterable, Sequence

import line_mentions
import reply_policy
from fact_hash import strip_format
from member_labels import BOT_NAME_VARIANTS, _clean_member_label

_BASE = Path(__file__).resolve().parent
PRIVACY_TERMS_ENV = "LINE_BOT_PRIVACY_TERMS_PATH"


class NameSourceUnavailable(RuntimeError):
    """require_names=True 卻讀不到本機名單：job 寧可不寫，也不讓名字進記憶。"""


# 2026-10-09 review: the extraction prompt asks the model to skip these; this is
# the deterministic backstop.  Facts are shown per person in the group
# (/看記憶) and go into every prompt, so it errs on dropping: 「在醫院上班」 is
# lost too.  2026-10-10 review: it missed 高血壓／癌症／月薪五萬／欠朋友錢 and
# half of family_weekly_insight's discard list; a test keeps it a superset.
PRIVATE_FACT_RE = re.compile(
    # 健康
    r"病|藥|診|醫|症|癌|瘤|手術|住院|開刀|化療|放療|洗腎|透析|失智|中風|血壓|血糖|膽固醇|糖尿|"
    r"心臟|過敏|氣喘|失眠|憂鬱|焦慮|恐慌|自殺|輕生|身心科|精神科|諮商|復健|"
    r"生理期|月經|經痛|更年期|懷孕|孕|流產|"
    # 感情與官司
    r"感情|吵架|冷戰|分手|男友|女友|男朋友|女朋友|交往|曖昧|外遇|劈腿|約會|離婚|告人|官司|訴訟|法院|"
    # 錢
    r"債|欠|借錢|貸|帳戶|存款|存了|有存|積蓄|儲蓄|密碼|薪|收入|保單|理賠|財產|資產|遺產|"
    r"[\d０-９一二兩三四五六七八九十百千]\s*萬|"
    # 2026-10-10 review 第三輪：審查員實測還會漏的寫法
    r"痛|炎|骨折|梗塞|心律|結石|胰島素|試管|體重|障|輪椅|戒酒|潰瘍|躁鬱|抑鬱|癲癇|帶原|開過刀|動過?刀|"
    r"產檢|預產期|坐月子|墮胎|有喜了|健檢|抽血|心理師|自殘|割腕|想不開|不想活|性侵|性騷擾|霸凌|"
    r"前夫|前妻|前任|出軌|小三|失戀|被甩|相親|分居|家暴|婚|單身|男票|女票|出櫃|"
    r"律師|被告|傳票|筆錄|判刑|假釋|和解|賠|坐牢|車禍|詐騙|被騙|"
    r"虧|破產|年終|獎金|退休金|月入|月領|年收|房租|裁員|失業|保險|刷爆|"
    r"卡號|身分證|身份證|字號|護照|帳號|"
    r"肝|長照|痔瘡|助聽器|失明|失禁|尿酸|發燒|看牙|洗牙|植牙|偷吃|不合|同志|坐過牢|套牢|勞退|零用錢|存錢|"
    r"月光|月退|樂透|(?<![A-Za-z])(?:BP|DM)(?![A-Za-z])|"
    r"[A-Za-z][12]\d{8}|\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}|\d+(?:\.\d+)?\s*[kKmM](?![A-Za-z])|"
    r"(?i:(?<![a-z])(?:cancer|pregnan\w*|depress\w*|anxiety|adhd|ivf|boyfriend|girlfriend|bf|gf|"
    r"salary|debt|loan|divorce\w*|lawsuit|sued|blood pressure|diabet\w*|insulin|therapy|covid|surgery)(?![a-z]))"
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# TODO(2026-10-16 deferred)：0086f5 10/16 補跑看到的本人事實缺口（民國NN年入學＝年齡、對政策的看法、家產與都更）
# 已向 Andrew 提出，等他決定要不要加進下面的窄類別。
# strict：只擋窄的寫法（「住在台中」「在台北上班」「每週日去游泳」都要留）。
# 第三欄是例外字：含「生日」的月日照收（生日提醒靠這種事實），但同一條有出生年份
# （四位數年份或民國 N 年）就照樣擋：年份等於年齡（2026-10-11 修正第一輪）。
_CN_NUM = "幾兩一二三四五六七八九十百千零〇"
_STRICT_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("money", re.compile(
        rf"(?:[0-9][0-9,.]*|[{_CN_NUM}]+)\s*(?:萬|千元|億|元|塊|美元|美金|台幣|日幣|張)"
        r"|(?i:NT\$)|\$\s*[0-9]"
    ), ""),
    # 2026-10-11 dry run：銀行往來、持有的卡、換匯收息、具名投資課程／群組都會被冒充理專或
    # 假投資群組拿去用（重建時的隱私審查也拿掉了這類）；「用悠遊卡」「用 iPhone」照收。
    ("banking", re.compile(
        r"銀行|貴賓|理專|匯率|換匯|收息|信用卡|簽帳卡|金融卡|聯名卡|刷卡|現金|付現"
        r"|(?i:[a-z]{2,})\s*卡|[0-9]+\s*投資|投資(?:群組|課程|社團|小白)|[0-9]{2,}\s*(?:課程|群組|社團)"
    ), ""),
    ("date", re.compile(
        r"(?:19|20)[0-9]{2}\s*[-/.年]\s*[0-9]{1,2}\s*[-/.月]\s*[0-9]{1,2}"
        r"|(?<![0-9])(?:1[0-2]|0?[1-9])\s*月\s*(?:3[01]|[12][0-9]|0?[1-9])\s*[日號号]"
        r"|[一二三四五六七八九十]{1,3}月[一二三四五六七八九十廿卅]{1,3}[日號号]"
        r"|(?<![0-9/])(?:1[0-2]|0?[1-9])/(?:3[01]|[12][0-9]|0?[1-9])(?![0-9])"
    ), "生日"),
    ("supplement", re.compile(r"保健|益生菌|魚油|(?i:nmn)|維他命|維生素|營養品|補充劑|葉黃素|膠原"), ""),
    ("age", re.compile(rf"敬老|[0-9{_CN_NUM}]+\s*歲|年紀"), ""),
    ("household", re.compile(r"戶籍"), ""),
    ("job_seeking", re.compile(r"求職|面試|履歷|找工作"), ""),
    ("politics", re.compile(r"民進黨|國民黨|民眾黨|藍白|綠營|藍營|政黨"), ""),
    # 2026-10-11 爸媽健康功能的審查員實測：睡不好、頭暈、跌倒、膝蓋、喘這類症狀原本會被收成一般記憶，
    # 繞過 /健康 的同意、暫停與刪除；這裡一律擋下（「痛」「發燒」等已在 PRIVATE_FACT_RE）。
    ("body", re.compile(
        r"失眠|睡不著|睡不好|淺眠|體重|減肥|瘦身|體脂|視力|頭暈|暈眩|跌倒|摔倒|滑倒|跌跤"
        r"|喘不過氣|呼吸困難|喘|膝蓋|關節|手麻|腳麻|胸悶|胸口|咳嗽|感冒|鼻塞|便秘|腹瀉|拉肚子"
    ), ""),
)
_BIRTH_YEAR_RE = re.compile(
    r"(?<![0-9])(?:19|20)[0-9]{2}(?![0-9])|民國\s*[0-9一二三四五六七八九十百]{1,4}\s*年"
)

# meta：每條事實都是提示裡的一行，不能變成給 bot 的指令或冒充程式的標記（GP2 #1）。
# 第二人稱的 lookbehind 留住「迷你豬」「迷你裙」；「系統工程師」「一律不吃辣」照收。
_META_RE = re.compile(
    "|".join(map(re.escape, BOT_NAME_VARIANTS))
    + r"|(?<!迷)[你妳您]|機器人|机器人|提示詞|提示词|指令|忽略|【|】"
    + r"|現在說話的是|现在说话的是|不確定是誰|不确定是谁|加賴|私訊|(?i:line\s*id)"
    # 去識別化留下的佔位字（每日 job 送模型前把連結、長數字換成這些）
    + r"|\[號碼\]|\[連結\]"
)
# family_weekly_insight._MESSAGE_LINK_RE 的副本（不 import 那個模組）
LINK_RE = re.compile(
    r"https?://\S+|www\.\S+|(?<![A-Za-z0-9-])[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?:/[!-~]*)?",
    re.IGNORECASE,
)
_HANDLE_RE = re.compile(r"@\w")
META_MAX_CHARS = 40

KINSHIP_TERMS = (
    "媽媽", "爸爸", "姊姊", "姐姐", "妹妹", "弟弟", "哥哥", "阿婆", "外婆", "奶奶", "阿嬤", "爺爺",
    "阿公", "外公", "兒子", "女兒", "老公", "老婆", "舅舅", "阿姨", "姑姑", "叔叔", "伯伯", "婆婆",
    "公公", "岳母", "媳婦", "女婿", "父親", "母親", "孫子", "孫女",
    "我媽", "我爸", "我哥", "我姊", "我姐", "我妹", "我弟",
)
_KINSHIP_RE = re.compile("|".join(map(re.escape, KINSHIP_TERMS)))
# 名單裡的這些不算名字（kinship 已經擋；全家這種也不是人名）
_GENERIC_TERMS = frozenset((*KINSHIP_TERMS, "全家", "大家"))


def _has_contact(text: str) -> bool:
    return bool(
        reply_policy._address_keys(text) or reply_policy._phone_keys(text) or _EMAIL_RE.search(text)
    )


def has_contact(text: str) -> bool:
    """地址、電話、email（回覆的聯絡資料關卡同一套）；原文、去格式字元、NFKC 三種寫法都看。"""
    raw = str(text or "")
    visible = strip_format(raw)
    return any(_has_contact(form) for form in (raw, visible, unicodedata.normalize("NFKC", visible)))


def _is_meta(text: str) -> bool:
    return (
        len(text) > META_MAX_CHARS
        or _META_RE.search(text) is not None
        or LINK_RE.search(text) is not None
        or _HANDLE_RE.search(text) is not None
    )


def _json_strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_strings(item)


def _terms_file(path: Path) -> list[str]:
    """LINE_BOT_PRIVACY_TERMS_PATH 指的檔：.json 取字串值，其他一行一個；沒有檔就是沒有名單。"""
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            return list(_json_strings(json.loads(text)))
    except (OSError, UnicodeError, ValueError):
        raise RuntimeError("privacy terms unreadable") from None
    return text.splitlines()


def _privacy_terms() -> list[str]:
    """本機隱私詞；讀不到（含載入不了 git_privacy_audit）一律丟 RuntimeError。"""
    override = os.environ.get(PRIVACY_TERMS_ENV, "").strip()
    if override:
        return _terms_file(Path(override))
    try:
        from jobs import git_privacy_audit  # 同一份本機隱私詞（不寫進 repo）
    except ImportError:
        raise RuntimeError("privacy terms loader unavailable") from None
    return list(git_privacy_audit.load_private_terms(_BASE))


def _require_alias_file() -> None:
    """user_aliases.json 要在、讀得到、至少有一個名字（load_user_aliases 讀不到只回 {}）。"""
    path = line_mentions._alias_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise NameSourceUnavailable("family alias file unavailable") from None
    if not isinstance(data, dict) or not any(isinstance(v, str) and v.strip() for v in data.values()):
        raise NameSourceUnavailable("family alias file has no names")


def _normalize_terms(raw: Iterable[str]) -> tuple[str, ...]:
    clean: set[str] = set()
    for value in raw:
        term = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
        if len(term) >= 2 and term not in _GENERIC_TERMS:
            clean.add(term)
    return tuple(sorted(clean, key=lambda item: (-len(item), item)))


def load_name_terms(*, require_names: bool = False) -> tuple[str, ...]:
    """本機名單（每次重讀、不快取）：隱私詞、別名／角色／短名、bot 記下的顯示名稱。

    require_names=True（job）：別名檔不在、讀不到、沒有名字，或隱私詞檔讀不到，就丟
    NameSourceUnavailable。require_names=False（即時抽取）：照常過濾，只是少擋名字。
    """
    if require_names:
        _require_alias_file()
    raw: list[str] = []
    try:
        raw.extend(_privacy_terms())
    except RuntimeError:
        if require_names:
            raise NameSourceUnavailable("privacy terms unreadable") from None
    try:
        mapping = line_mentions.configured_family_alias_mapping(include_short=True)
        raw.extend(mapping)
        raw.extend(mapping.values())
    except Exception:
        if require_names:
            raise NameSourceUnavailable("family alias mapping unavailable") from None
    try:
        for name in line_mentions.display_names().values():
            raw.extend((name, _clean_member_label(name)))
    except Exception:
        pass  # 顯示名稱檔不在是正常的（沒有查過任何人）
    return _normalize_terms(raw)


def private_text(text: str) -> bool:
    """這段原文送去 Gemini 之前要不要先拿掉（2026-10-17 每日記憶 Gemini 備援）。

    健康與私人細節（PRIVATE_FACT_RE）、身體症狀、金額、銀行與卡、地址電話 email——這些事實本來就不會存，
    原文也就不必送去 Gemini 免費版。只是減少送出，不是保證：沒列到的寫法照樣會送。
    """
    raw = str(text or "")
    visible = strip_format(raw)
    norm = unicodedata.normalize("NFKC", visible)
    if any(PRIVATE_FACT_RE.search(form) for form in (raw, visible, norm)):
        return True
    if has_contact(raw):
        return True
    withheld = ("body", "money", "banking", "politics", "age", "job_seeking", "supplement", "household")
    return any(pattern.search(norm) for category, pattern, _exempt in _STRICT_RULES if category in withheld)


def reject_reason(
    fact: str, *, require_names: bool = False, names: Sequence[str] | None = None
) -> str:
    """"" = 可以存；否則回類別（private、contact、strict:<類別>、meta、kinship、name）。

    ``names`` 是 load_name_terms() 的結果（同一批事實只讀一次名單）；沒給就當場讀。
    """
    raw = str(fact or "")
    visible = strip_format(raw)  # 零寬、bidi 這類看不見的字不能拿來拆開關鍵字
    text = unicodedata.normalize("NFKC", visible)
    if not text.strip():
        return "empty"
    if any(PRIVATE_FACT_RE.search(form) for form in (text, visible, raw)):
        return "private"
    if has_contact(raw):
        return "contact"
    for category, pattern, exempt in _STRICT_RULES:
        if exempt and exempt in text:
            if _BIRTH_YEAR_RE.search(text):
                return f"strict:{category}"
            continue
        if pattern.search(text):
            return f"strict:{category}"
    if _is_meta(text):
        return "meta"
    if _KINSHIP_RE.search(text):
        return "kinship"
    terms = load_name_terms(require_names=require_names) if names is None else names
    folded = text.casefold()
    if any(term and term in folded for term in terms):
        return "name"
    return ""
