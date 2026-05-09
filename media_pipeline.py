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

    # Layer 1：本機 vision LLM 抽描述（raw bytes 不離本機）
    desc = None
    try:
        from vision_llm import describe_image
        desc = describe_image(image, prompt=prompt)
    except ImportError:
        logger.info("vision_llm 未建")
    except Exception as e:
        logger.warning("vision_llm failed: %s", e)

    if not desc or not desc.strip():
        # 沒描述 → 至少回 OCR
        if ocr_text:
            return f"📷 OCR 抽到的文字：\n{ocr_text[:800]}\n\n（vision LLM 不可用，僅 OCR 結果）"
        return None

    # Layer 2：Hybrid B — 用描述 + 多源 web search → Gemini 用既有規則包裝
    # （只送描述文字，圖片 raw bytes 永遠不出本機）
    import os as _os
    if _os.environ.get("MEDIA_HYBRID_DISABLED") != "1":
        try:
            wrapped = _wrap_with_gemini_news_style(desc, ocr_text or "")
            if wrapped:
                return wrapped
        except Exception as e:
            logger.warning("hybrid B wrap failed, fallback to raw desc: %s", e)

    # Layer 3：純 vision_llm 描述（不 wrap）
    return desc


def _wrap_with_gemini_news_style(desc: str, ocr_text: str = "") -> Optional[str]:
    """B mode：本機描述 + 多源 search → Gemini 用 _CORE_PROMPT + _RULE_NEWS_CASE 包裝。

    quota 爆 → 回 None 讓 caller fallback 純 desc。
    """
    # Step 1: 抽 topic 跑 web search（純本機爬蟲，多來源 8+ 筆）
    sources_block = ""
    sources_with_url = []  # 給 prompt 強制要求 model 在 reply 帶 URL
    try:
        from web_scraper import search_duckduckgo, search_google_news, search_wiki_full
        topic = (desc[:80] + " " + ocr_text[:50]).strip()

        # DDG 5 筆
        try:
            for r in search_duckduckgo(topic, k=5):
                if r.get("title") and r.get("url"):
                    sources_with_url.append(r)
        except Exception as e:
            logger.info("DDG fail: %s", e)

        # Google News 5 筆
        try:
            for r in search_google_news(topic, k=5):
                if r.get("title") and r.get("url"):
                    sources_with_url.append(r)
        except Exception as e:
            logger.info("GoogleNews fail: %s", e)

        # Wiki（如可）
        try:
            wiki_q = topic.split()[0] if topic else None
            if wiki_q:
                wiki = search_wiki_full(wiki_q)
                if wiki and wiki.get("extract"):
                    sources_with_url.append({
                        "title": wiki.get("title", "Wikipedia"),
                        "url": wiki.get("url", "https://zh.wikipedia.org/"),
                        "snippet": wiki.get("extract", "")[:200],
                    })
        except Exception:
            pass

        # 去 dup（by url）
        seen = set()
        deduped = []
        for r in sources_with_url:
            u = r["url"]
            if u not in seen:
                seen.add(u)
                deduped.append(r)
        sources_with_url = deduped[:10]  # cap 10

        # 拼成 prompt block
        sources_block = "\n".join(
            f"[{i+1}] {r['title'][:80]}\n     URL: {r['url']}\n     {r.get('snippet','')[:200]}"
            for i, r in enumerate(sources_with_url)
        )
    except Exception as e:
        logger.info("web search skip: %s", e)

    # Step 2: 寫 reply — Gemini 主路（質量好），quota 爆 → 本機 14B fallback（無 quota）
    user_msg = (
        f"LINE 群有人貼了一張圖，請用咪寶風回覆。\n\n"
        f"【圖片描述】\n{desc}\n\n"
        f"【相關 sources（編號跟你引用對應，每條已含 URL）】\n{sources_block or '(沒抓到 sources)'}\n\n"
        f"結構（敘述體，不要章節標題）：\n"
        f"1. **第一段**：直接給你的整合判斷（規則 0，第一句就要 take）\n"
        f"2. **第二段**：正方/支持方在說什麼，引 [1][2] 等對應 sources\n"
        f"3. **第三段**：反方/質疑方在說什麼，引 [3][4] 等對應 sources\n"
        f"4. **第四段**：整合 — 綜合兩方後你的判斷（含具體數字 / 機制 / 為什麼）\n"
        f"5. **第五段**：實用建議或警告（1-2 句）\n"
        f"6. **最後一段**：來源 — 列至少 4 條，格式 `機構名 https://URL`，每條一行\n\n"
        f"硬性規則：\n"
        f"- 必須引用至少 4 個 sources（用 [1][2] 編號對應上面 sources 區塊）\n"
        f"- 來源段必須含實際 URL（從上面 sources 直接複製，不要編造、不要短化）\n"
        f"- 不要「同意的部分 / 反對的部分 / 判斷依據 / 結論」這種 section header\n"
        f"- 不要「希望對您有幫助 / 以上僅供參考」結尾\n"
        f"- 整體用流暢敘述體，不要列表化標題化\n"
        f"- 忠實 follow sources（sources 沒提的不要憑記憶補）\n\n"
        f"直接給回覆，不要前綴。"
    )

    # 主路：Gemini
    try:
        from gemini_client import chat
        out = chat(user_msg, [], [])
        if out and out.strip():
            return out.strip()
    except Exception as e:
        logger.info("Gemini wrap quota / err: %s", e)

    # Fallback：本機 14B（無 quota）
    logger.info("Gemini 不可用 → fallback 本機 14B 寫 reply")
    try:
        from local_llm import chat as local_chat
        meibao_system = (
            "你是 LINE 群組對話助理咪寶。風格：繁體中文、第一句具體判斷、編號實用點、機制 + 數字、簡短列來源。"
            "禁止 formal section header（同意/反對/判斷依據/結論）。禁止「希望對您有幫助」結尾。"
        )
        out = local_chat(user_msg, system_prompt=meibao_system, max_tokens=800)
        return out.strip() if out else None
    except Exception as e:
        logger.warning("local_llm wrap also failed: %s", e)
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
