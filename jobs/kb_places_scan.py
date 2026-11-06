"""kb_places_scan — 從家人訊息找出地點知識庫的候選（2026-10-31，第一批段一）。

Andrew 10/30：「從對話中自行抓取」。知識庫查證排程（line-bot-kb-places-verify）跑這支：找出提到店家、郵局、機關的
電話或地址，或點名問咪寶「某某店電話？」的訊息，列給排程，由排程認出店名、上網查證，
再交給 kb_places_admin。

讀法二選一，都不連網、不寫入正式檔：
- ``--from-production``：自己把正式 line_bot.db 連同同名的 -wal、-shm 抄進私人暫存資料夾（0700），
  用一般連線開副本（WAL 副本沒有能用的 -shm 時 mode=ro 開不起來）；quick_check 不是 ok 就刪掉重抄
  一次，再不行就不掃。不論成敗，掃完整個暫存資料夾刪掉。排程跑這個就不用自己碰 DB。
- ``--db``：讀已經抄好的副本（mode=ro）；指到正式檔時拒絕。
輸出只給排程自己的對話看，不要寫進報告：每筆只有群組代號、訊息編號、日期和一小段文字；
網址換成「[連結]」、地址換成「[地址]」、四碼以上的數字換成「[號碼]」（比照 fact_filters 的去識別化寫法）。

整則跳過：bot 自己的訊息、指令、含密碼帳號卡號這類字或像卡號的長數字、藏有看不見字元的訊息。
那一整句跳過：提到私人住家、工作地點、寄送地址、自己的手機、親屬「某某家」（寧可少抓）。
那一個子句跳過：醫療院所（第 2 批）、銀行（第 3 批）。
一個子句（逗號也切）要同時有「店家類字眼」和電話或地址才算；「電話／地址／位於…」開頭的
子句可以接上一個子句的店名（「甲麵館，電話 02-…」）。沒有店家字眼的手機號碼一律不收。

2026-10-31 實作審查（審查員 1、3）後補：逗號切子句、地址也遮、住家和工作地點的說法、
親屬「某某家」、醫療科別、像卡號的數字整則跳過；店家字眼只留一份（敘述和問句共用）。
收回清單（``--skip-file``）段二起才有；檔案不存在時照常全部掃描，檔案壞了就整個不掃。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import sqlite3
import stat
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_normalize as kbn  # noqa: E402

_TW = ZoneInfo("Asia/Taipei")
BOT_USER = "__bot__"
SNIPPET_MAX = 80
MAX_PER_MESSAGE = 3
DEFAULT_HOURS = 48.0
MAX_HOURS = 168.0
PRODUCTION_DB = Path(__file__).resolve().parents[1] / "line_bot.db"
COPY_PREFIX = "kb_scan_"
STALE_COPY_SECONDS = 3600
_SIDECARS = ("-wal", "-shm")

_LEADING_MENTION_RE = re.compile(r"^(?:@\S+\s*)+")
_SENTENCE_RE = re.compile(r"[^。！？!?\n；;]+")
_CLAUSE_SPLIT_RE = re.compile(r"[，,、]")
SENSITIVE_RE = re.compile(
    r"密碼|帳號|帳戶|卡號|信用卡|金融卡|身分證|身份證|居留證|護照號碼|健保卡號|保單|驗證碼|OTP|PIN碼|戶名|匯款|轉帳",
    re.I,
)
# 16 碼左右、分組寫的長數字（卡號、帳號）：整則跳過。
_CARD_LIKE_RE = re.compile(r"(?<![0-9])[0-9]{4}[\s\-]?[0-9]{4}[\s\-]?[0-9]{4}(?:[\s\-]?[0-9]{2,4})?(?![0-9])")
_KIN = "|".join(map(re.escape, kbn.KINSHIP_TERMS))
PERSONAL_RE = re.compile(
    r"我家|你家|他家|她家|我們家|你們家|他們家|老家|住家|住址|住在|家住|我住|搬到|搬去|宿舍|租屋|家裡|"
    r"新地址|舊地址|送到|寄到|收件|收貨|我公司|公司在|上班|辦公室|我的手機|我手機|打給我|打我|聯絡我|找我|"
    rf"我電話|(?:{_KIN}|阿嬤|阿公|朋友|同學|同事|親戚)家"
)
# 店家字眼：敘述（「甲麵館電話 02-…」）和點名問句（「@咪寶 那家火鍋電話？」）共用這一份（審查員 4）。
BUSINESS_RE = re.compile(
    r"店|館|餐廳|小吃|美食|咖啡|早餐|便當|火鍋|燒肉|牛排|甜點|烘焙|飲料|超市|超商|賣場|"
    r"市場|百貨|商場|門市|五金|洗衣|修車|機車行|車行|眼鏡|書局|文具|花店|美髮|理髮|髮廊|美容|健身房|"
    r"郵局|區公所|鄉公所|鎮公所|市公所|戶政|地政|監理|國稅局|稽徵所|派出所|運動中心"
)
_CONTINUATION_RE = re.compile(r"^\s*(?:電話|地址|位於|位在|就在|在|住址)")
PLACE_QUESTION_RE = re.compile(r"電話|地址|在哪|在哪裡|在那裡|怎麼去|位置")
_LONG_NUMBER_RE = re.compile(r"[0-9][0-9\s\-.()]{2,}[0-9]")
BOT_NAME = "咪寶"


@dataclass(frozen=True)
class Candidate:
    group_id: str
    message_id: str
    kind: str  # statement：說了電話或地址；question：點名問地點
    day: str
    snippet: str


# 遮地址用：容許中文數字和空白（「重慶南路 一段 122 號」）。
_ADDRESS_MASK_RE = re.compile(
    r"[一-鿿]{1,8}" + kbn.ROAD_SUFFIX + r"[\s0-9〇零一二兩三四五六七八九十百段巷弄之\-]*?號(?:之[0-9]+)?"
)


def _mask(clause: str) -> str:
    text = kbn.LINK_RE.sub("[連結]", kbn.nfkc(clause))
    text = _ADDRESS_MASK_RE.sub("[地址]", text)

    def digits(match: re.Match) -> str:
        return "[號碼]" if len(re.sub(r"[^0-9]", "", match.group(0))) >= 4 else match.group(0)

    text = _LONG_NUMBER_RE.sub(digits, text)
    return re.sub(r"\s+", " ", text).strip()[:SNIPPET_MAX]


def _has_address(clause: str) -> bool:
    return bool(kbn.ADDRESS_HINT_RE.search(kbn.address_text(clause)))


def _skip_clause(clause: str) -> bool:
    return bool(kbn.MEDICAL_RE.search(clause) or kbn.BANK_RE.search(clause))


def classify(text: str) -> list[tuple[str, str]]:
    """一則訊息裡的候選子句：[(kind, 遮過的片段)]。整則要跳過時回 []。"""
    if not text or not text.strip() or kbn.has_hidden_chars(text, emoji_ok=True):
        return []
    body = _LEADING_MENTION_RE.sub("", text.strip())
    if body.startswith(("/", "／")) or SENSITIVE_RE.search(text) or _CARD_LIKE_RE.search(kbn.nfkc(text)):
        return []
    addressed = BOT_NAME in text
    no_links = kbn.LINK_RE.sub(" ", kbn.nfkc(text))
    found: list[tuple[str, str]] = []
    for sentence in _SENTENCE_RE.findall(no_links):
        if not sentence.strip() or PERSONAL_RE.search(sentence):
            continue
        clauses = [c for c in _CLAUSE_SPLIT_RE.split(sentence) if c.strip()]
        previous = ""
        for clause in clauses:
            if _skip_clause(clause):
                previous = ""
                continue
            has_contact = bool(kbn.find_phones(clause)) or _has_address(clause)
            business_here = BUSINESS_RE.search(clause) is not None
            business_before = bool(previous) and _CONTINUATION_RE.match(clause) is not None
            if has_contact and (business_here or business_before):
                found.append(("statement", _mask((previous + "，" if business_before and not business_here else "") + clause)))
            elif addressed and PLACE_QUESTION_RE.search(clause) and business_here:
                found.append(("question", _mask(clause)))
            previous = clause if business_here else ""
    return found


def _skip_set(path: str | None) -> set[tuple[str, str]]:
    """段二起 bot 會把收回的訊息記在 state/kb_unsent.json；段一還沒有這個檔。讀不到就整個不掃。"""
    if not path or not Path(path).exists():
        return set()
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    return {(str(r["group_id"]), str(r["message_id"])) for r in rows}


def scan(conn: sqlite3.Connection, *, since: int, until: int, skip: set[tuple[str, str]] = frozenset()) -> list[Candidate]:
    rows = conn.execute(
        "SELECT group_id, message_id, text, created_at FROM raw_messages "
        "WHERE user_id != ? AND created_at BETWEEN ? AND ? ORDER BY created_at",
        (BOT_USER, since, until),
    ).fetchall()
    found: list[Candidate] = []
    seen: set[tuple[str, str]] = set()
    for group_id, message_id, text, created_at in rows:
        ref = (str(group_id), str(message_id))
        if ref in skip or ref in seen:
            continue
        seen.add(ref)
        day = datetime.fromtimestamp(int(created_at), _TW).strftime("%Y-%m-%d")
        # 一則訊息最多列 3 個子句：同一則提到兩家店時，兩句都要給排程看。
        for kind, snippet in list(dict.fromkeys(classify(text or "")))[:MAX_PER_MESSAGE]:
            found.append(Candidate(ref[0], ref[1], kind, day, snippet))
    return found


class CopyNotOk(Exception):
    """正式 DB 抄了兩次，quick_check 都不是 ok。"""


def _copy_private(src: Path, dst: Path) -> None:
    # 先開來源再建目的檔（0600、不覆蓋）：來源剛好不見時不留空檔。
    with open(src, "rb") as inp:
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(inp, out)


def _copy_db(src: Path, dst: Path) -> None:
    """DB 檔＋同名的 -wal、-shm（有才抄）→ dst。只當一般檔案讀，不用 SQLite 開來源。"""
    _copy_private(src, dst)
    for suffix in _SIDECARS:
        try:
            _copy_private(Path(f"{src}{suffix}"), Path(f"{dst}{suffix}"))
        except FileNotFoundError:
            continue  # 沒有，或剛好被 checkpoint 收掉


def _quick_check_ok(conn: sqlite3.Connection) -> bool:
    return conn.execute("PRAGMA quick_check").fetchall() == [("ok",)]


def _open_production_copy(src: Path, workdir: Path) -> sqlite3.Connection:
    """把 src 抄進 workdir，用一般連線開副本（不用 mode=ro：WAL 副本沒有能用的 -shm 會開不起來）。
    quick_check 不是 ok：關掉、刪掉副本、重抄一次；兩次都不行丟 CopyNotOk。"""
    src = src.resolve()  # 符號連結時 -wal、-shm 在真正的檔旁邊
    dst = workdir / "snapshot.db"
    for _ in range(2):
        _copy_db(src, dst)
        conn = sqlite3.connect(str(dst))
        ok = False
        try:
            ok = _quick_check_ok(conn)
        except sqlite3.Error:
            pass  # 抄到一半的檔可能直接報 malformed：當成不是 ok
        finally:
            if not ok:
                conn.close()
        if ok:
            return conn
        for leftover in workdir.iterdir():
            leftover.unlink()
    raise CopyNotOk


def _copy_root() -> Path:
    return Path(tempfile.gettempdir())


def _remove_stale_copies(root: Path, now: float) -> None:
    """上次被砍掉（例如 Bash 逾時被 SIGKILL）沒刪成的副本：只清自己的、超過一小時的 kb_scan_* 資料夾。"""
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for entry in entries:
        if not entry.name.startswith(COPY_PREFIX):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and now - info.st_mtime >= STALE_COPY_SECONDS:
            shutil.rmtree(entry.path, ignore_errors=True)


def _exit_on_signal(signum, frame):
    # 讓 with 區塊照常收尾：SIGTERM／SIGHUP（Bash 逾時、排程被停）時也會刪掉暫存副本
    raise SystemExit(128 + signum)


def _scan_production(src: Path, *, since: int, until: int, skip: set[tuple[str, str]]) -> list[Candidate]:
    """抄一份私人副本來掃；不論成敗，先關連線再刪掉整個暫存資料夾。"""
    root = _copy_root()
    _remove_stale_copies(root, time.time())
    with tempfile.TemporaryDirectory(prefix=COPY_PREFIX, dir=root) as workdir:  # mkdtemp：0700
        conn = _open_production_copy(src, Path(workdir))
        try:
            return scan(conn, since=since, until=until, skip=skip)
        finally:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--db", help="line_bot.db 的副本（不要指向正式檔）")
    source.add_argument(
        "--from-production", action="store_true",
        help="自己把正式 line_bot.db（含 -wal、-shm）抄進私人暫存資料夾再掃，掃完刪掉",
    )
    parser.add_argument("--hours", type=float, default=DEFAULT_HOURS)
    parser.add_argument("--skip-file", default=None, help="收回清單（段二起才有）")
    parser.add_argument("--now", type=int, default=None, help="測試用：現在的 epoch 秒（只影響掃描的時間範圍）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    now = args.now if args.now is not None else int(time.time())
    if not 0 < args.hours <= MAX_HOURS:
        print(f"kb_places_scan: --hours 要在 0–{MAX_HOURS:g} 之間", file=sys.stderr)
        return 2
    if args.db is not None and Path(args.db).resolve() == PRODUCTION_DB.resolve():
        print("kb_places_scan: 不准直接讀正式 line_bot.db，請用 DB 副本", file=sys.stderr)
        return 2
    if args.from_production and not PRODUCTION_DB.is_file():
        print("kb_places_scan: 找不到正式 line_bot.db，這次不掃", file=sys.stderr)
        return 2
    try:
        skip = _skip_set(args.skip_file)
    except (OSError, ValueError, KeyError, TypeError):
        print("kb_places_scan: 收回清單讀不到，這次整個不掃", file=sys.stderr)
        return 2
    since = now - int(args.hours * 3600)
    try:
        if args.from_production:
            old = {sig: signal.signal(sig, _exit_on_signal) for sig in (signal.SIGTERM, signal.SIGHUP)}
            try:
                found = _scan_production(PRODUCTION_DB, since=since, until=now, skip=skip)
            finally:
                for sig, handler in old.items():
                    signal.signal(sig, handler)
        else:
            conn = sqlite3.connect(Path(args.db).resolve().as_uri() + "?mode=ro", uri=True)
            found = scan(conn, since=since, until=now, skip=skip)
    except CopyNotOk:
        print("kb_places_scan: 正式 DB 抄了兩次，quick_check 都不是 ok，這次不掃", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"kb_places_scan: 正式 DB 的暫存副本出錯（{type(exc).__name__}），這次不掃", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"kb_places_scan: 讀不到 DB 副本（{type(exc).__name__}）", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([asdict(c) for c in found], ensure_ascii=False))
        return 0
    if not found:
        print("這段時間沒有地點候選。")
        return 0
    for c in found:
        print(f"{c.day}｜{c.kind}｜{c.group_id}:{c.message_id}｜{c.snippet}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
