"""OCR helper — 圖片中文字抽取（中文 + 英文）。

Lazy load EasyOCR（首次 ~30 sec download model）。
"""
from __future__ import annotations
from io import BytesIO
import logging
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
        _reader = easyocr.Reader(
            ['ch_tra', 'en'], gpu=False, download_enabled=False
        )  # mac no cuda; cached local models only
        logger.info("EasyOCR loaded (ch_tra + en)")
        return True
    except Exception as e:
        logger.warning("EasyOCR load failed type=%s", type(e).__name__)
        # fallback to tesseract
        try:
            __import__("pytesseract")  # availability check only
            logger.info("fallback to pytesseract")
            _reader = "tesseract"
            return True
        except Exception:
            return False


def extract_text(image_path: str | bytes, min_confidence: float = 0.3) -> Optional[str]:
    """從圖片抽文字。回 str（多行 \\n 分隔）or None。"""
    if not _ensure_loaded():
        return None

    try:
        if _reader == "tesseract":
            # pytesseract materializes PIL inputs as delete=False temp files.
            # Raw webhook bytes must remain memory-only; skip OCR and let the
            # local vision path handle them instead.
            if isinstance(image_path, bytes):
                logger.info("skip pytesseract for in-memory image bytes")
                return None
            import pytesseract
            from PIL import Image
            with Image.open(image_path) as image:
                text = pytesseract.image_to_string(
                    image,
                    lang="chi_tra+eng",
                )
            return text.strip() if text.strip() else None
        # easyocr
        if isinstance(image_path, bytes):
            import numpy as np
            from PIL import Image

            with Image.open(BytesIO(image_path)) as opened:
                source = np.asarray(opened.convert("RGB"))
        else:
            source = str(image_path)
        results = _reader.readtext(source)
        lines = [
            text for (_, text, conf) in results
            if conf >= min_confidence and text.strip()
        ]
        if not lines:
            return None
        return "\n".join(lines)
    except Exception as e:
        logger.warning("extract_text failed type=%s", type(e).__name__)
        return None
