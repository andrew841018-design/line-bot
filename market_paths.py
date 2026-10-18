"""市場盤勢（/盤勢，2026-10-17）的檔案路徑與私有檔案工具。

Andrew 2026-10-17 要一張「目前市場狀況」的盤勢卡（美股／台股的技術面、寬度、量能、期權、籌碼、
槓桿、13F、宏觀、事件）。重活在 launchd 的 jobs/market_snapshot.py 裡做，bot 只讀它寫好的快照；
兩邊用同一份路徑設定，測試用 `Paths.under(tmp)` 把全部檔案搬到暫存目錄。

規則（審查 GP2 N4、N12、I6）：
- 不建立、不 chmod `state/` 本身：restart_lock.sh 要求它是自己的 0700，被改到整個 bot 的重啟鎖會失效。
  不合格就不寫，由呼叫端記 outcome。
- 圖目錄只有一套檢查（`private_dir_ok`）：不是 symlink、owner＝自己、沒有 group／other 權限；
  不存在才用 0700 建立；別人的目錄一律不碰、不服務。
- 私有檔一律「同目錄暫存檔（O_EXCL）＋fsync＋rename」寫入，權限 0600；讀取用 O_NOFOLLOW、只收
  自己的一般檔、有大小上限。

這個模組 import 時不做任何 I/O。
"""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_BOT_DIR = Path(__file__).resolve().parent
_CHART_DIR = Path("/tmp/line_bot_genimg")  # 既有的 /static/img/{32hex}.png 路由從這裡讀


@dataclass(frozen=True)
class Paths:
    state_dir: Path
    history_db: Path
    snapshot: Path
    worker_state: Path
    outcome: Path
    lock: Path
    disabled: Path  # job 停用旗標：存在就不抓、不發布
    dashboard_disabled: Path  # /盤勢 回覆停用旗標：存在就回「盤勢：暫停使用」
    deployed_hash: Path  # 部署腳本寫的 market_*.py sha256；job 啟動時比對
    chart_dir: Path
    seasonality: Path  # repo 內的靜態季節性表

    @classmethod
    def default(cls) -> "Paths":
        return cls._build(_BOT_DIR / "state", _CHART_DIR, _BOT_DIR / "market_seasonality.json")

    @classmethod
    def under(cls, root: Path) -> "Paths":
        root = Path(root)
        return cls._build(root / "state", root / "genimg", root / "market_seasonality.json")

    @classmethod
    def _build(cls, state: Path, charts: Path, seasonality: Path) -> "Paths":
        return cls(
            state_dir=state,
            history_db=state / "market_history.sqlite",
            snapshot=state / "market_dashboard.json",
            worker_state=state / "market_snapshot_worker.json",
            outcome=state / "market_snapshot_outcome.json",
            lock=state / "market_snapshot.lock",
            disabled=state / "market_snapshot.disabled",
            dashboard_disabled=state / "market_dashboard.disabled",
            deployed_hash=state / "market_deployed.sha256",
            chart_dir=charts,
            seasonality=seasonality,
        )


def _own_private(info: os.stat_result) -> bool:
    return info.st_uid == os.getuid() and (info.st_mode & 0o077) == 0


def state_dir_ok(path: Path) -> bool:
    """`state/` 要已經存在、不是 symlink、是自己的、沒有 group／other 權限；這裡只看不改。"""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and _own_private(info)


def private_dir_ok(path: Path, *, create: bool = True) -> bool:
    """圖目錄這類私有目錄：不是 symlink、owner＝自己、沒有 group／other 權限。

    不存在且 create=True 時用 0700 建立（只建最後一層，不建上層）。已存在但不合格就回 False，
    不去 chmod：可能是別的帳號在 /tmp 先建的。
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if not create:
            return False
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        except OSError:
            return False
        try:
            info = os.lstat(path)
        except OSError:
            return False
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and _own_private(info)


def write_private_atomic(path: Path, data: bytes) -> None:
    """同目錄暫存檔（mkstemp＝O_EXCL、0600）→ 寫入 → fsync → rename → fsync 目錄。

    失敗時刪掉暫存檔並把例外往上丟；目錄本身不建立。
    """
    directory = path.parent
    fd, tmp = tempfile.mkstemp(prefix=".market-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def read_private_file(path: Path, max_bytes: int) -> Optional[tuple[bytes, tuple[int, int, int]]]:
    """讀自己的私有一般檔：O_NOFOLLOW、S_ISREG、owner＝自己、大小 ≤ max_bytes。

    回 (內容, (st_ino, st_mtime_ns, st_size))；不存在或不合格回 None（不丟例外）。
    """
    try:
        # O_NONBLOCK：路徑上若被放了 FIFO，開檔不會卡住（一般檔不受影響；下面再確認是一般檔）。
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > max_bytes:
            return None
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                return None
            chunks.append(chunk)
        return b"".join(chunks), (info.st_ino, info.st_mtime_ns, info.st_size)
    except OSError:
        return None
    finally:
        os.close(fd)


def flag_set(path: Path) -> bool:
    """停用旗標：存在就是停用；除了「不存在」以外的錯誤（權限、I/O）也一律當成停用。"""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True
