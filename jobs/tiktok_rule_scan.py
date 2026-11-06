"""tiktok_rule_scan — 數 DB 副本裡可能叫咪寶忽略 TikTok 的規則（2026-10-31）。

2026-04-15：學到的規則裡有「忽略 TikTok」，咪寶就不看家人傳的 TikTok 連結。以前由
test_prefetch.py::test_db_clean 直接開測試檔旁邊的 line_bot.db 檢查；測試不能讀家庭對話 DB，
worktree 裡也沒有 DB（那支測試每次都失敗），改成每日維護對 DB 副本跑這支。

只讀 ``--db`` 指定的副本（mode=ro），拒絕正式路徑；不連網、不寫入、不 import bot 的模組
（import memory 會在正式目錄建 DB、config 會讀 .env）。只輸出一行筆數，不輸出規則內容或群組 id；
要不要清由 Andrew 決定。讀不到副本回 2，訊息只有例外類別。

「會進提示詞」照 canonical 糾正記憶（預設開）：已連結的 organic 糾正只以 correction_rules 的文字
進提示詞，note 本身是不能刪的稽核列。``CORRECTION_CANONICAL_MEMORY_ENABLED=0``（回滾開關）時，
稽核列會以原文進提示詞，要當成「會進提示詞」看；這支不讀 .env，不自己判斷。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
# test_prefetch.py 的 JS 網站清單裡的三個 TikTok 網址，加網頁版短網址、圖文（main.py 的
# _TIKTOK_PHOTO_PATH_RE）、帶分享參數的影片：skip 規則擋掉任何一個，咪寶就不看那則 TikTok。
TIKTOK_URLS = (
    "https://vt.tiktok.com/ZSHsUffnc/",
    "https://vm.tiktok.com/abc123/",
    "https://www.tiktok.com/@user/video/123",
    "https://www.tiktok.com/t/ZTabc123/",
    "https://www.tiktok.com/@user/photo/7300000000000000001",
    "https://www.tiktok.com/@user/video/7300000000000000002?is_from_webapp=1&sender_device=pc",
    "https://www.tiktok.com/@user/video/7300000000000000003?_t=8sKXyZ&_r=1",
)
_TIKTOK_WORDS = ("tiktok", "抖音", "douyin")
_IGNORE_WORD = "忽略"


def norm(text: str | None) -> str:
    """只留文字和數字（NFKC、casefold 之後）：全形、大小寫、空白、標點、零寬字元都不影響比對，
    「ＴｉｋＴｏｋ」「Tik Tok」「tik-tok」「Tik‧Tok」都算 tiktok。"""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text or "").casefold() if ch.isalnum())


def mentions_tiktok(text: str | None) -> bool:
    folded = norm(text)
    return any(word in folded for word in _TIKTOK_WORDS)


def mentions_ignore(text: str | None) -> bool:
    return _IGNORE_WORD in norm(text)


def _match_rules(text: str, rules: list[dict]) -> str | None:
    """burst_filter._match_rules 的複本（這支不能 import burst_filter；測試對照兩邊）。"""
    must_hit = False
    skip_hit = False
    for r in rules:
        pattern = r.get("pattern", "")
        if not pattern:
            continue
        if pattern in text:
            if r["kind"] == "must_answer":
                must_hit = True
            elif r["kind"] == "skip":
                skip_hit = True
    if must_hit:
        return "must_answer"
    if skip_hit:
        return "skip"
    return None


def reaches_prompt(kind: str, source: str, linked: int) -> bool:
    """memory.list_persona_notes_for_prompt（canonical 模式）會把這筆 note 的原文送進提示詞嗎（測試對照兩邊）。"""
    return kind == "example" or source != "organic" or not linked


def scan(conn: sqlite3.Connection) -> dict[str, int]:
    """各類筆數。缺表或缺欄位直接丟 sqlite3.Error，不當成 0 筆。"""
    counts = dict.fromkeys(
        ("prompt_tiktok", "prompt_ignore", "audit_tiktok", "audit_ignore",
         "skip_tiktok", "skip_blocks", "must_tiktok", "rule_tiktok", "rule_ignore"),
        0,
    )
    for kind, source, linked, scenario, content in conn.execute(
        "SELECT kind, source, correction_linked, scenario, content FROM persona_notes"
    ):
        where = "prompt" if reaches_prompt(kind, source, linked) else "audit"
        text = f"{scenario or ''} {content or ''}"
        counts[f"{where}_tiktok"] += mentions_tiktok(text)
        counts[f"{where}_ignore"] += mentions_ignore(text)

    by_group: dict[str, list[dict]] = defaultdict(list)
    for group_id, kind, pattern in conn.execute("SELECT group_id, kind, pattern FROM filter_rules"):
        by_group[group_id].append({"kind": kind, "pattern": pattern or ""})
        if mentions_tiktok(pattern):
            counts["skip_tiktok" if kind == "skip" else "must_tiktok"] += 1
    for rules in by_group.values():
        blocked = [url for url in TIKTOK_URLS if _match_rules(url, rules) == "skip"]
        counts["skip_blocks"] += sum(
            1 for r in rules if r["kind"] == "skip" and r["pattern"] and any(r["pattern"] in url for url in blocked)
        )

    for (rule,) in conn.execute("SELECT canonical_rule FROM correction_rules WHERE status = 'active'"):
        counts["rule_tiktok"] += mentions_tiktok(rule)
        counts["rule_ignore"] += mentions_ignore(rule)
    return counts


# 稽核列與 must_answer（保護性的規則）只列數字，不算「要 Andrew 看」。
_INFO_ONLY = ("audit_tiktok", "audit_ignore", "must_tiktok")


def render(c: dict[str, int]) -> str:
    verdict = "要 Andrew 看" if any(n for key, n in c.items() if key not in _INFO_ONLY) else "沒有"
    return (
        f"TikTok 忽略規則：{verdict}（"
        f"persona_notes 會進提示詞：TikTok {c['prompt_tiktok']}、忽略 {c['prompt_ignore']}；"
        f"persona_notes 稽核列：TikTok {c['audit_tiktok']}、忽略 {c['audit_ignore']}；"
        f"filter_rules：skip 提到 TikTok {c['skip_tiktok']}、skip 會擋 TikTok 連結 {c['skip_blocks']}、"
        f"must_answer 提到 TikTok {c['must_tiktok']}；"
        f"correction_rules（啟用中）：TikTok {c['rule_tiktok']}、忽略 {c['rule_ignore']}）。"
    )


def _is_live(path: Path) -> bool:
    """正式 DB、或任何一份 bot checkout 裡的檔，一律不讀（只收每日維護 cp 到暫存目錄的副本）。``path`` 已 resolve。

    比 jobs/learned_rules.is_live_path 嚴：這支所在的 checkout 底下（含 state/）、上層資料夾裡有
    main.py 和 memory.py 的（例如從 clone 跑卻給了正式 DB 的路徑）、正式 DB 與 SQLITE_PATH（含 hard link）
    都算；不 import config。
    """
    if path.is_relative_to(BASE):
        return True
    for folder in path.parents:
        if all((folder / name).exists() for name in ("main.py", "memory.py")):
            return True
    lives = [BASE / "line_bot.db"]
    if os.environ.get("SQLITE_PATH"):
        lives.append(Path(os.environ["SQLITE_PATH"]).expanduser().resolve())
    return any(path == live or _same(path, live) for live in lives)


def _same(a: Path, b: Path) -> bool:
    """同一個檔（hard link、大小寫不同的路徑都算）。"""
    return a.exists() and b.exists() and os.path.samefile(a, b)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="line_bot.db 的副本（每日維護第 1 點 cp 的那份）")
    args = parser.parse_args(argv)
    try:
        path = Path(args.db).expanduser().resolve()
        if _is_live(path):
            print("tiktok_rule_scan: 不讀正式 DB 或 bot 程式目錄裡的檔，請給暫存目錄裡的副本", file=sys.stderr)
            return 2
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            counts = scan(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"tiktok_rule_scan: 讀不到 DB 副本（{type(exc).__name__}）", file=sys.stderr)
        return 2
    except Exception as exc:  # 例如欄位存成 BLOB、`--db ~沒有這個人/x.db`：traceback 會帶出內容，stderr 只留一行類別
        print(f"tiktok_rule_scan: 掃描失敗（{type(exc).__name__}）", file=sys.stderr)
        return 2
    print(render(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
