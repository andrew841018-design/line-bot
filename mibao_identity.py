"""Canonical visual identity for the LINE bot persona Mibao.

The reference photo remains a private local asset. The current image-generation
backends are text-to-image only, so the descriptor improves resemblance but
does not guarantee that generated images depict the exact same cat.
"""

from __future__ import annotations

import hashlib
import re
import stat
from pathlib import Path


IDENTITY_VERSION = 1
NAME = "咪寶"
ALIASES = ("咪寶", "Mibao")

CHAT_IDENTITY_ZH = (
    "你是 LINE 群組助理咪寶。你的固定外觀是一隻棕灰色短毛虎斑貓，"
    "有綠色大眼、淺色口鼻與下巴。"
)
VISION_IDENTITY_ZH = (
    "你是咪寶——住在 LINE 群組裡的助理；你的固定外觀是一隻真實虎斑貓。"
)
APPEARANCE_ZH = (
    "棕灰色短毛虎斑貓，綠色大眼、深色額頭條紋、直立尖耳、"
    "粉棕色鼻子、淺色口鼻與下巴、白色長鬍鬚"
)
IMAGE_PROMPT_EN = (
    "Mibao, a petite brown-gray mackerel tabby domestic shorthair cat with "
    "large round green eyes, dark forehead stripes, upright pointed ears, "
    "a pinkish-brown nose, a pale cream muzzle and chin, and long white whiskers"
)

PRIVATE_ASSET_DIR = Path(__file__).resolve().parent.parent / "private_persona_assets"
REFERENCE_IMAGE_PATH = PRIVATE_ASSET_DIR / "mibao_reference.jpg"
REFERENCE_IMAGE_SHA256 = (
    "5e5727e07839e03b1be1512e28fb4ea6be97fb35520c119a214d83d8cfbec8c1"
)
GENERATION_FIDELITY = "text_prompt_approximation"


def verify_reference_image() -> tuple[bool, str]:
    """Validate the deployed private reference without following symlinks."""
    path = REFERENCE_IMAGE_PATH
    if path.parent != PRIVATE_ASSET_DIR:
        return False, "reference path escaped the private asset directory"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False, "reference image is missing"
    if stat.S_ISLNK(info.st_mode):
        return False, "reference image must not be a symlink"
    if not stat.S_ISREG(info.st_mode):
        return False, "reference image is not a regular file"
    if stat.S_IMODE(info.st_mode) != 0o600:
        return False, "reference image permissions must be 0600"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != REFERENCE_IMAGE_SHA256:
        return False, "reference image hash mismatch"
    return True, "ok"


def is_mibao_subject(text: str) -> bool:
    """Return whether an image subject explicitly names Mibao.

    Chinese matching excludes the complete common phrase ``貓咪寶寶`` without
    rejecting a legitimate name such as ``小貓咪寶``. English uses ASCII token
    boundaries so values such as ``mibaox`` do not match.
    """
    subject = text or ""
    if NAME in subject.replace("貓咪寶寶", ""):
        return True
    return bool(re.search(r"(?<![A-Za-z0-9_])mibao(?![A-Za-z0-9_])", subject, re.I))


def is_exact_mibao_alias(text: str) -> bool:
    """Return whether text consists only of one canonical Mibao alias."""
    value = (text or "").strip("，,、。！!？?：: \t")
    return value == NAME or value.casefold() == "mibao"


def augment_image_prompt(original_subject: str, translated_subject: str) -> str:
    """Append Mibao's descriptor only when the raw subject names her."""
    if not is_mibao_subject(original_subject):
        return translated_subject
    if IMAGE_PROMPT_EN in translated_subject:
        return translated_subject
    return f"{translated_subject}; canonical appearance: {IMAGE_PROMPT_EN}"
