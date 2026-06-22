"""
knowledge_graph.py — 純本機 knowledge graph 萃取（zero cloud API）。

從中文對話自動建立 (subject, relation, object) 三元組，存進 SQLite。
100% 本機運算，純規則式 + jieba.posseg 詞性標註。

主要 API:
    extract_triples(text)               → list[(s, r, o)]   萃取三元組
    store_triples(group_id, triples)    → int               寫入 SQLite，回新增筆數
    query_triples(group_id, ...)        → list[dict]        查詢
    auto_extract_kg_async(group_id, t)  → fire-and-forget   非同步抽 + 寫

涵蓋 patterns:
    - 「我（的）X 是 Y」「我有 X」「我在 X」「我喜歡 X」「我叫 X」
    - 「我老婆/太太/小孩 X 是 Y」 → (spouse/child, has_X, Y) + 同義詞合併
    - 「X 是 Y」（X 為專名 / nr / nt） → (X, is_a, Y)
    - 「X 在 Y 工作 / 出生 / 居住 / 唸書」
    - 「X 的生日是 Y」「X 的電話是 Y」 等屬性句
    - jieba.posseg nr (人名) / ns (地名) / nt (機構) 詞性 + regex 配合

設計原則:
    - 規則手刻、可解釋、Andrew 隨時加 patterns
    - 不依雲端模型 → 永遠免費 + 隱私 100%
    - subject="user" 代表「我 / 我的」自述（從 user_id 維度區分留待 v2）
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time as _time
import warnings
from pathlib import Path

# jieba 在 Python 3.13 會跳 pkg_resources DeprecationWarning，靜音
warnings.filterwarnings("ignore", message=".*pkg_resources.*")

import jieba  # noqa: E402
import jieba.posseg as pseg  # noqa: E402

from config import settings  # noqa: E402

jieba.setLogLevel(60)

# 把常見親屬詞加進 jieba dict，避免被切錯
_KIN_USERWORDS: tuple[str, ...] = (
    "老婆", "老公", "太太", "妻子", "丈夫", "先生", "妹妹", "弟弟", "哥哥", "姊姊",
    "媽媽", "爸爸", "母親", "父親", "兒子", "女兒", "孩子", "小孩",
    "外婆", "外公", "奶奶", "爺爺", "阿嬤", "阿公",
    "信義區", "大安區", "中山區", "松山區", "板橋區", "中正區", "士林區",
    "內湖區", "南港區", "文山區", "北投區",
)
for _w in _KIN_USERWORDS:
    jieba.add_word(_w)


# ─────────────────────────────────────────────────────────────────────────────
# 同義詞 / 親屬關係映射 (~30 條)
# ─────────────────────────────────────────────────────────────────────────────
_SYNONYMS: dict[str, str] = {
    # 配偶
    "老婆": "spouse",
    "太太": "spouse",
    "妻子": "spouse",
    "老公": "spouse",
    "丈夫": "spouse",
    "先生": "spouse",
    "另一半": "spouse",
    # 父母
    "媽媽": "mother",
    "母親": "mother",
    "媽": "mother",
    "老媽": "mother",
    "爸爸": "father",
    "父親": "father",
    "爸": "father",
    "老爸": "father",
    # 子女
    "兒子": "son",
    "女兒": "daughter",
    "小孩": "child",
    "孩子": "child",
    "寶寶": "child",
    # 兄弟姊妹
    "哥哥": "elder_brother",
    "弟弟": "younger_brother",
    "姊姊": "elder_sister",
    "妹妹": "younger_sister",
    # 祖父母
    "爺爺": "grandfather_paternal",
    "奶奶": "grandmother_paternal",
    "外公": "grandfather_maternal",
    "外婆": "grandmother_maternal",
    "阿公": "grandfather_paternal",
    "阿嬤": "grandmother_paternal",
    # 工作 / 朋友
    "同事": "colleague",
    "朋友": "friend",
    "好友": "friend",
    "閨蜜": "close_friend",
    "老闆": "boss",
    "主管": "boss",
    "同學": "classmate",
}


def merge_synonyms(token: str) -> str:
    """把親屬詞 / 關係詞正規化成英文 key，沒命中就原樣回傳。"""
    if not token:
        return token
    return _SYNONYMS.get(token.strip(), token.strip())


# ─────────────────────────────────────────────────────────────────────────────
# Regex pattern table — 順序很重要：較具體的規則放前面
# 每條 pattern 產生 0..n 個 (subject, relation, object)
# ─────────────────────────────────────────────────────────────────────────────

# 親屬 + 屬性句：「我老婆生日是 5/14」「我兒子的名字叫小明」
# group(1) = 親屬詞，group(2) = 屬性，group(3) = 值
_KIN_ATTR = re.compile(
    r"我(?:的)?(老婆|太太|妻子|老公|丈夫|先生|另一半|"
    r"媽媽|母親|爸爸|父親|"
    r"兒子|女兒|小孩|孩子|寶寶|"
    r"哥哥|弟弟|姊姊|妹妹|"
    r"爺爺|奶奶|外公|外婆|阿公|阿嬤|"
    r"同事|朋友|閨蜜|老闆|主管|同學)"
    r"(?:的)?(生日|名字|電話|手機|生肖|星座|血型|身高|體重|工作|職業|公司|學校|興趣|愛好)"
    r"(?:是|叫|為|＝|=)\s*([^\s，。！？!?、,.;]{1,40})"
)

# 我自己的屬性句：「我生日是 5/14」「我的名字叫 Andrew」
_USER_ATTR = re.compile(
    r"我(?:的)?(生日|名字|電話|手機|生肖|星座|血型|身高|體重|工作|職業|公司|學校|興趣|愛好|綽號|email|信箱|住址|地址)"
    r"(?:是|叫|為|＝|=)\s*([^\s，。！？!?、,.;]{1,40})"
)

# 「我有 X」 → (user, has, X)
_USER_HAS = re.compile(
    r"我(?:現在)?有(?:一隻|一個|一隻|一輛|一台|一支)?\s*([^\s，。！？!?、,.;]{1,30})"
)

# 「我在 X 工作 / 上班 / 唸書 / 讀書」
_USER_AT_VERB = re.compile(
    r"我(?:現在)?(?:目前)?在\s*([^\s，。！？!?、,.;]{1,30}?)\s*(工作|上班|唸書|讀書|生活|住|居住)"
)

# 「我住在 X」「我住 X」
_USER_LIVE = re.compile(
    r"我(?:現在)?住(?:在)?\s*([^\s，。！？!?、,.;]{1,30})"
)

# 「我是 X」 → (user, is_a, X)；排除掉常見 noise（「我是說」「我是覺得」）
_USER_IS = re.compile(
    r"^我(?:就)?是\s*((?!說|覺得|認為|想|覺|在|不|沒|有|要)[^\s，。！？!?、,.;]{1,30})"
)

# 「我喜歡/討厭/愛吃 X」
_USER_LIKE = re.compile(
    r"我(很|超|非常|蠻|挺)?(喜歡|討厭|愛|愛吃|愛喝|不喜歡|不愛|害怕|怕)\s*([^\s，。！？!?、,.;]{1,30})"
)

# 「我叫 X」 / 「我的名字是 X」 - 已被 _USER_ATTR 涵蓋

# 「X 是 Y」其中 X 為專名（人名/地名/機構名 詞性 nr/ns/nt）— 透過 posseg 後處理
_GENERIC_IS = re.compile(r"([^\s，。！？!?、,.;]{2,15})是([^\s，。！？!?、,.;]{1,30})")

# 「X 在 Y 工作 / 出生 / 上班」 - 兩端都需是專名
_GENERIC_AT_VERB = re.compile(
    r"([^\s，。！？!?、,.;]{2,15})在([^\s，。！？!?、,.;]{2,30}?)(工作|上班|出生|居住|住|讀書|唸書)"
)


def _is_proper_noun(text: str) -> bool:
    """jieba.posseg 判斷一個 token 是否包含 nr/ns/nt 詞性。"""
    if not text or len(text) < 2:
        return False
    try:
        for word, flag in pseg.lcut(text):
            if flag in ("nr", "ns", "nt") and len(word) >= 2:
                return True
    except Exception:
        return False
    return False


def _clean_obj(s: str) -> str:
    """把 object 邊緣的標點 / 助詞清掉。"""
    s = s.strip().strip("的了嗎呢吧啊喔嘛")
    return s.strip()


# ─────────────────────────────────────────────────────────────────────────────
# 主萃取函式
# ─────────────────────────────────────────────────────────────────────────────


def extract_triples(text: str) -> list[tuple[str, str, str]]:
    """從一句中文文字抽出 (subject, relation, object) 三元組。

    回傳已 dedupe 的 list；空字串 / 過短文字回 []。
    """
    if not text or len(text.strip()) < 3:
        return []

    triples: list[tuple[str, str, str]] = []
    raw = text.strip()

    # 1. 親屬 + 屬性：「我老婆生日是 5/14」 → (spouse, birthday, 5/14)
    for m in _KIN_ATTR.finditer(raw):
        kin, attr, val = m.group(1), m.group(2), _clean_obj(m.group(3))
        if val:
            rel = f"{merge_synonyms(kin)}_{_attr_to_en(attr)}"
            triples.append(("user", rel, val))

    # 2. 我自己屬性：「我生日是 5/14」 → (user, birthday, 5/14)
    for m in _USER_ATTR.finditer(raw):
        attr, val = m.group(1), _clean_obj(m.group(2))
        if val:
            triples.append(("user", _attr_to_en(attr), val))

    # 3. 「我有 X」 → (user, has, X)
    for m in _USER_HAS.finditer(raw):
        obj = _clean_obj(m.group(1))
        if obj and len(obj) >= 1:
            triples.append(("user", "has", obj))

    # 4. 「我在 X 工作」 → (user, work_at / live_at / study_at, X)
    for m in _USER_AT_VERB.finditer(raw):
        place, verb = _clean_obj(m.group(1)), m.group(2)
        if place:
            rel = {
                "工作": "work_at", "上班": "work_at",
                "唸書": "study_at", "讀書": "study_at",
                "生活": "live_at", "住": "live_at", "居住": "live_at",
            }.get(verb, "at")
            triples.append(("user", rel, place))

    # 5. 「我住 X」 → (user, live_at, X)
    for m in _USER_LIVE.finditer(raw):
        place = _clean_obj(m.group(1))
        if place:
            triples.append(("user", "live_at", place))

    # 6. 「我是 X」 → (user, is_a, X)
    for m in _USER_IS.finditer(raw):
        obj = _clean_obj(m.group(1))
        if obj and obj not in ("說", "覺得", "認為", "想"):
            triples.append(("user", "is_a", obj))

    # 7. 「我喜歡 X」 → (user, like / dislike, X)
    for m in _USER_LIKE.finditer(raw):
        verb, obj = m.group(2), _clean_obj(m.group(3))
        if obj:
            rel = {
                "喜歡": "like", "愛": "like", "愛吃": "like_food",
                "愛喝": "like_drink",
                "討厭": "dislike", "不喜歡": "dislike", "不愛": "dislike",
                "害怕": "fear", "怕": "fear",
            }.get(verb, "like")
            triples.append(("user", rel, obj))

    # 8. 「X 在 Y 工作 / 出生」 — X 必須是專名
    for m in _GENERIC_AT_VERB.finditer(raw):
        s, place, verb = m.group(1), _clean_obj(m.group(2)), m.group(3)
        if not place:
            continue
        # 過濾「我」開頭（已在前面處理）
        if s.startswith("我"):
            continue
        if _is_proper_noun(s):
            rel = {
                "工作": "work_at", "上班": "work_at", "出生": "born_at",
                "居住": "live_at", "住": "live_at",
                "讀書": "study_at", "唸書": "study_at",
            }.get(verb, "at")
            triples.append((s, rel, place))

    # 9. 通用「X 是 Y」 — X 必須是專名才接受（避免「天氣是好」這種垃圾）
    for m in _GENERIC_IS.finditer(raw):
        s, o = m.group(1), _clean_obj(m.group(2))
        if not s or not o:
            continue
        if s.startswith("我") or o.startswith("我"):
            continue
        # 跳過已在前面處理的句型開頭
        if any(s.startswith(k) for k in (
            "生日", "名字", "電話", "手機", "我的", "你的", "他的"
        )):
            continue
        if _is_proper_noun(s):
            triples.append((s, "is_a", o))

    # dedupe，保持順序
    seen: set[tuple[str, str, str]] = set()
    unique: list[tuple[str, str, str]] = []
    for t in triples:
        # 標準化 + clean
        s, r, o = t[0].strip(), t[1].strip(), t[2].strip()
        if not s or not r or not o:
            continue
        if len(o) > 60:  # object 太長八成是切錯
            continue
        key = (s, r, o)
        if key not in seen:
            seen.add(key)
            unique.append(key)

    return unique


def _attr_to_en(attr: str) -> str:
    """中文屬性詞 → 英文 relation key。"""
    mapping = {
        "生日": "birthday", "名字": "name", "電話": "phone", "手機": "phone",
        "生肖": "zodiac_animal", "星座": "zodiac",
        "血型": "blood_type", "身高": "height", "體重": "weight",
        "工作": "job", "職業": "job", "公司": "company",
        "學校": "school", "興趣": "interest", "愛好": "interest",
        "綽號": "nickname", "email": "email", "信箱": "email",
        "住址": "address", "地址": "address",
    }
    return mapping.get(attr, attr)


# ─────────────────────────────────────────────────────────────────────────────
# SQLite — kg_triples table
# ─────────────────────────────────────────────────────────────────────────────

_DB_PATH = Path(settings.sqlite_path)
_lock = threading.Lock()


def _db_path(db_path: Path | str | None = None) -> Path:
    return Path(db_path) if db_path is not None else _DB_PATH


def _conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(db_path), isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_table(db_path: Path | str | None = None) -> None:
    """確保 kg_triples table 存在（同 memory.py 那邊也會做一次，這裡是 defensive）。"""
    with _lock, _conn(db_path) as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS kg_triples (
                group_id    TEXT NOT NULL,
                subject     TEXT NOT NULL,
                relation    TEXT NOT NULL,
                object      TEXT NOT NULL,
                source_text TEXT,
                created_at  INTEGER NOT NULL,
                PRIMARY KEY (group_id, subject, relation, object)
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_triples_subject "
            "ON kg_triples(group_id, subject)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_triples_relation "
            "ON kg_triples(group_id, relation)"
        )


_ensure_table()


def store_triples(
    group_id: str,
    triples: list[tuple[str, str, str]],
    source_text: str = "",
    db_path: Path | str | None = None,
) -> int:
    """寫入 kg_triples，回真正新增的筆數（PRIMARY KEY 衝突會 IGNORE）。"""
    if not triples:
        return 0
    _ensure_table(db_path)
    now = int(_time.time())
    added = 0
    with _lock, _conn(db_path) as c:
        for s, r, o in triples:
            cur = c.execute(
                "INSERT OR IGNORE INTO kg_triples"
                "(group_id, subject, relation, object, source_text, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (group_id, s, r, o, source_text[:500], now),
            )
            if cur.rowcount > 0:
                added += 1
    return added


def query_triples(
    group_id: str,
    subject: str | None = None,
    relation: str | None = None,
    object: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """查詢 kg_triples。任一過濾條件 None 就不過濾。"""
    sql = "SELECT subject, relation, object, source_text, created_at FROM kg_triples WHERE group_id = ?"
    args: list = [group_id]
    if subject is not None:
        sql += " AND subject = ?"
        args.append(subject)
    if relation is not None:
        sql += " AND relation = ?"
        args.append(relation)
    if object is not None:
        sql += " AND object = ?"
        args.append(object)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        rows = c.execute(sql, args).fetchall()
    return [
        {
            "subject": r[0],
            "relation": r[1],
            "object": r[2],
            "source_text": r[3] or "",
            "created_at": r[4],
        }
        for r in rows
    ]


def clear_triples(group_id: str) -> int:
    """清空某 group 的 triples，回刪除筆數。給測試用。"""
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM kg_triples WHERE group_id = ?", (group_id,))
        return cur.rowcount


# ─────────────────────────────────────────────────────────────────────────────
# Async fire-and-forget helper
# ─────────────────────────────────────────────────────────────────────────────


def auto_extract_kg_async(group_id: str, text: str) -> None:
    """背景執行緒抽 + 寫，不阻塞 caller。失敗 silently swallow。"""
    if not group_id or not text:
        return
    db_path = _DB_PATH

    def _run() -> None:
        try:
            triples = extract_triples(text)
            if triples:
                store_triples(group_id, triples, source_text=text, db_path=db_path)
        except Exception:
            # 永不阻塞主流程
            pass

    threading.Thread(target=_run, daemon=True, name="kg-extract").start()
