"""媒體（圖片 / 影片）處理 pipeline — 純本機，零雲端 LLM。

組合：
  圖片：OCR 抽文字 → 本機 vision LLM → 本機失敗 → OCR-only fallback
  影片：抽 keyframes → 本機多圖 vision LLM → 本機失敗 → 沉默

跟 main.py 既有 Gemini Vision (handle image / video event) 平行。
quota 爆時 main 改 call 這邊。
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("media_pipeline")


def analyze_image(
    image: str | Path | bytes,
    user_prompt: str = "",
) -> Optional[str]:
    """對圖片產生回應。失敗回 None。

    流程：
      1. OCR 抽文字（如果有 ocr_helper）
      2. 本機 Vision LLM（mlx-vlm Qwen2.5-VL-7B）看圖 + 用 OCR 文字輔助 prompt
      3. 本機失敗 → 至少回 OCR 文字（零雲端 fallback）
    """
    # OCR
    ocr_text = None
    try:
        from ocr_helper import extract_text
        ocr_text = extract_text(image)
        if ocr_text:
            logger.info("OCR 抽到 %d chars", len(ocr_text))
    except ImportError:
        logger.info("ocr_helper 未建，跳過 OCR")
    except Exception as e:
        logger.warning("OCR failed: %s", e)

    # 拼 prompt
    prompt = (
        user_prompt
        or "請用繁體中文描述這張圖的內容、主題、可見文字。重點清楚、簡短。"
    )
    if ocr_text:
        prompt += f"\n\n圖中已 OCR 抽到的文字（參考）：\n{ocr_text[:500]}"

    # Layer 1：本機 vision LLM
    try:
        from vision_llm import describe_image
        out = describe_image(image, prompt=prompt)
        if out and out.strip():
            return out
        logger.info("本機 vision_llm 回空，退到 OCR-only fallback")
    except ImportError:
        logger.info("vision_llm 未建")
    except Exception as e:
        logger.warning("vision_llm failed: %s", e)

    # Layer 2：至少回 OCR 內容
    if ocr_text:
        return f"📷 OCR 抽到的文字：\n{ocr_text[:800]}\n\n（vision LLM 不可用，僅 OCR 結果）"
    return None


def analyze_video(
    video: str | Path | bytes,
    user_prompt: str = "",
) -> Optional[str]:
    """對影片產生回應。失敗回 None。

    流程：
      1. 抽 keyframes（max 6）
      2. 本機多圖 vision LLM 看
      3. 本機失敗 → None（沉默）
    """
    # Keyframes
    frames = []
    try:
        from video_keyframes import extract_keyframes
        frames = extract_keyframes(video, max_frames=6)
        if not frames:
            logger.info("沒抽到 keyframes")
            return None
        logger.info("抽到 %d frames", len(frames))
    except ImportError:
        logger.info("video_keyframes 未建")
        return None
    except Exception as e:
        logger.warning("extract_keyframes failed: %s", e)
        return None

    prompt = (
        user_prompt
        or f"以下是一段影片的 {len(frames)} 個關鍵畫面。請用繁體中文摘要影片內容、可能的主題、有什麼可看到的文字。重點清楚、簡短。"
    )

    out: Optional[str] = None
    try:
        # 本機 vision LLM 多圖
        try:
            from vision_llm import chat_with_images
            out = chat_with_images(prompt, frames, max_tokens=600)
            if out and out.strip():
                logger.info("vision_llm chat_with_images 成功")
            else:
                out = None
                logger.info("本機 vision_llm 回空")
        except ImportError:
            logger.info("vision_llm 未建")
        except Exception as e:
            logger.warning("vision_llm chat_with_images failed: %s", e)
    finally:
        try:
            from video_keyframes import cleanup as _cleanup
            _cleanup(frames)
        except Exception:
            pass

    return out


if __name__ == "__main__":
    # smoke test：自造一張圖
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (400, 100), 'white')
    d = ImageDraw.Draw(img)
    # ASCII-only to avoid default-font CJK encoding issues across envs
    d.text((20, 30), "Test Image 123", fill='black')
    img.save('/tmp/media_test.jpg')

    print("\n>>> analyze_image('/tmp/media_test.jpg')")
    out = analyze_image('/tmp/media_test.jpg', user_prompt="這張圖在說什麼？")
    print(out or "(None — 模組未全部就緒)")
