"""fact_hash — 家人記憶事實的 keyed hash 與正規化（2026-10-11 每日記憶）。

ledger 和 /忘記 留下的 tombstone 只存 HMAC-SHA256，不存事實原文：只拿到 ledger 的人
也猜不出「住在台中」這種短句（GP2 #6）。

金鑰：state/fact_hash.key（0600，secrets.token_bytes(32) 原子建立），路徑可用
LINE_BOT_FACT_HASH_KEY_PATH 覆寫（測試會覆寫）。uvicorn（/忘記 寫 tombstone、即時抽取寫
ledger）和每日 job 讀同一個檔，各程序一定拿到同一把；也不依賴 JOBS_MASTER_TOKEN，
token 輪替不會讓舊的 tombstone 和 ledger 對不上。（uvicorn 其實也有 .env：main.py 匯入時
就 load_dotenv。）

這個模組不碰 DB：什麼時候可以建新金鑰、金鑰有沒有換掉，由 memory.fact_hash_key() 用 DB 裡
記下的指紋把關——金鑰檔不見或被換掉時丟 FactHashKeyMismatch，絕不默默重建（2026-10-11
修正第一輪 #4）。

正規化（fact_store 存的寫法、tombstone 與去重比對共用）：先去掉零寬等格式字元（Cf），
再 NFKC、收成一行、去掉句尾的。．.！!；;（保留」》）這類收尾）。canonical() 再去空白、
不分大小寫，只用來比對。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import unicodedata
from pathlib import Path

from speaker_turns import one_line

logger = logging.getLogger(__name__)

KEY_PATH_ENV = "LINE_BOT_FACT_HASH_KEY_PATH"
_DEFAULT_KEY_PATH = Path(__file__).resolve().with_name("state") / "fact_hash.key"
_KEY_BYTES = 32
_PURPOSE = b"fact-ledger"
_FINGERPRINT_LABEL = b"fingerprint"
_FINGERPRINT_CHARS = 16
_TRAILING_PUNCT = "。．.！!；; \t"


class FactHashKeyMismatch(RuntimeError):
    """金鑰檔不見或被換掉（和 DB 記下的指紋不同）：舊的 tombstone 和 ledger 都會對不上，
    所以不寫入、不重建，交給人處理。"""


def key_path() -> Path:
    return Path(os.environ.get(KEY_PATH_ENV) or _DEFAULT_KEY_PATH)


def read_secret() -> bytes | None:
    """金鑰檔的內容；檔案不在回 None，讀不到或太短丟 OSError。不會建立新檔。"""
    try:
        secret = key_path().read_bytes()
    except FileNotFoundError:
        return None
    if len(secret) < _KEY_BYTES:
        raise OSError("fact hash key is too short")
    return secret


def create_secret() -> bytes:
    """建立金鑰檔並回傳內容（只有 memory.fact_hash_key 在 DB 還沒有指紋時呼叫）。

    寫好暫存檔再 link 成正式檔：另一個程序同時建立時，大家用先建好的那一把，
    已經存在的金鑰絕不覆蓋。
    """
    path = key_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            handle = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
        with handle:
            handle.write(secrets.token_bytes(_KEY_BYTES))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)
            logger.warning("fact hash key created")  # 只在第一次部署時出現；之後出現代表金鑰被重建過
        except FileExistsError:
            pass
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    secret = read_secret()
    if secret is None:
        raise OSError("fact hash key could not be created")
    return secret


def derive_key(secret: bytes) -> bytes:
    """這個用途（ledger 與 tombstone）的 HMAC 金鑰。"""
    return hmac.new(secret, _PURPOSE, hashlib.sha256).digest()


def fingerprint(key: bytes) -> str:
    """存進 DB 的金鑰指紋：HMAC(key, "fingerprint") 的前 16 碼，看不出金鑰本身。"""
    return hmac.new(key, _FINGERPRINT_LABEL, hashlib.sha256).hexdigest()[:_FINGERPRINT_CHARS]


def strip_format(text: str) -> str:
    """去掉零寬空白、bidi 控制這類看不見的格式字元（Unicode 類別 Cf）。"""
    return "".join(ch for ch in str(text or "") if unicodedata.category(ch) != "Cf")


def normalize_text(text: str) -> str:
    """存進記憶的寫法：去格式字元、NFKC、一行、去句尾標點。"""
    normalized = unicodedata.normalize("NFKC", strip_format(text))
    return one_line(normalized).rstrip(_TRAILING_PUNCT).strip()


def canonical(text: str) -> str:
    """比對用：normalize_text 之後去掉所有空白、不分大小寫。"""
    return "".join(normalize_text(text).split()).casefold()


def fact_hmac(group_id: str, user_id: str, text: str, *, key: bytes) -> str:
    """HMAC-SHA256(key, [group_id, user_id, text]) 的 hex；text 照原樣（呼叫端決定要不要正規化）。

    key 一定要由呼叫端給（memory.fact_hash_key()）：這裡不會自己去讀或建金鑰。
    """
    message = json.dumps([group_id or "", user_id or "", text or ""], ensure_ascii=False)
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()
