"""Private folders under the system temp dir.

2026-11-06: $TMPDIR had two million entries, and code looking for its own leftovers
listed all of them before every Claude CLI call.  A folder of its own keeps that short.
vision_llm._PrivateIpcStore.ensure_private_root does the same checks with its own
error type; a fix to one probably belongs in the other.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def private_tmp_dir(name: str) -> Path:
    """``<temp dir>/<name>-<uid>``, created 0700; OSError when it is not ours.

    Read on every call: the folder can go away under a running bot, and a test's
    ``tempfile.tempdir`` moves it.
    """
    root = Path(tempfile.gettempdir()) / f"{name}-{os.getuid()}"
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        pass
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        logger.warning("private temp folder failed ownership validation name=%s", name)
        raise PermissionError(f"{name}: temp folder failed ownership validation")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        os.chmod(root, 0o700, follow_symlinks=False)
    return root
