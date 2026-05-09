"""OCR helper — 圖片中文字抽取（中文 + 英文）。

Lazy load EasyOCR（首次 ~30 sec download model）。
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ocr_helper")

_reader = None


def _ensure_loaded() -> bool:
    global _reader
    if _reader is not None:
        return True
    try:
        import easyocr
        # 中英 dual-language
        _reader = easyocr.Reader(['ch_tra', 'en'], gpu=False)  # mac no cuda
        logger.info("EasyOCR loaded (ch_tra + en)")
        return True
    except Exception as e:
        logger.warning("EasyOCR load failed: %s", e)
        # fallback to tesseract
        try:
            import pytesseract  # noqa
            logger.info("fallback to pytesseract")
            _reader = "tesseract"
            return True
        except Exception:
            return False


def extract_text(image_path: str | bytes, min_confidence: float = 0.3) -> Optional[str]:
    """從圖片抽文字。回 str（多行 \\n 分隔）or None。"""
    if not _ensure_loaded():
        return None

    # bytes → temp file
    if isinstance(image_path, bytes):
        import tempfile
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        tmp.write(image_path)
        tmp.close()
        image_path = tmp.name

    try:
        if _reader == "tesseract":
            import pytesseract
            from PIL import Image
            text = pytesseract.image_to_string(
                Image.open(image_path),
                lang="chi_tra+eng",
            )
            return text.strip() if text.strip() else None
        # easyocr
        results = _reader.readtext(str(image_path))
        lines = [
            text for (_, text, conf) in results
            if conf >= min_confidence and text.strip()
        ]
        if not lines:
            return None
        return "\n".join(lines)
    except Exception as e:
        logger.warning("extract_text failed: %s", e)
        return None
