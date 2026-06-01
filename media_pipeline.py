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


# ── Phase 1 media_cache helpers (byte-exact dedup, group-scoped) ───────────
#
# Phase 1.5 deferred (per advisor family-bot threat model recalibration):
#   - In-flight dedup（immediate handler + drain worker 跑同 sha 同 race）
#     — family-bot 5 人 rare event, accept；Phase 1.5 加 Python module-level
#     `_inflight: dict[(media_type, sha), Event]` + try/finally
#   - PII regex on description / Reply quality gate / DoS limits
#     — wrong threat model (family chat 互看 PII，非 adversarial); enterprise scope
#   - cache_version 沒加：見 memory.insert_media_cache docstring，改 prompt
#     後手動 DELETE invalidate

_MIN_CACHE_BYTES = 1024       # 太小檔不算（preview / corrupted / sticker）
_MIN_CACHE_REPLY_LEN = 200    # 太短 reply 不 cache（L3 raw desc 通常 < 200 字）

_local_llm_down_alerted = False  # once-per-process 告警 guard；成功生回應時 reset（見 _respond_to_ocr_text）


def _alert_local_llm_down(reason: str) -> None:
    """本機模型全載不進來（多半 ~/.cache/huggingface symlink 斷 / 外接碟沒掛）→ 通知 Andrew 一次。

    fail-soft：絕不拋例外影響回覆；process 內去重避免洗版。
    reason 一律當不可信字串：strip mention + 截長，避免任何 caller 不慎洩漏內容 / @everyone 轟炸。
    """
    global _local_llm_down_alerted
    if _local_llm_down_alerted:
        return
    _local_llm_down_alerted = True
    safe = str(reason or "").replace("@everyone", "@ everyone").replace("@here", "@ here")[:80]
    try:
        import notify_discord
        notify_discord.send_dm(
            "⚠️ LINE bot 本機 AI 模型載不進來（" + safe + "）。"
            "圖片目前只能回降級訊息。請檢查外接碟 WD_BLACK 是否沒接 / "
            "~/.cache/huggingface symlink 是否斷掉。"
        )
    except Exception as e:
        logger.warning("_alert_local_llm_down: notify failed: %s", e)


def _respond_to_ocr_text(ocr_text: str) -> Optional[str]:
    """本機 vision 描述不可用時：把 OCR 文字餵本機 14B，針對『內容』生回應（非 echo / 非裸吐）。

    單次快速本機呼叫（無 web search / grounding / critique），塞得進 _handle_image_message
    的 50s reply 視窗。回傳前過 vision_common.post_check 對齊規則 0 / 黑名單。
    本機 LLM 不可用（模型載不進來）回 None，交給 caller 走降級訊息 + 告警。
    """
    global _local_llm_down_alerted
    text = (ocr_text or "").strip()
    if not text:
        return None
    try:
        from local_llm import chat as local_chat
    except Exception as e:
        logger.warning("_respond_to_ocr_text: local_llm import failed: %s", e)
        return None
    # 文字模式咪寶 prompt。不用 vision_common.compose_prompt：那是「看圖」取向、且會把內容
    # 塞進 system 又與 user_input 重複（codex NIT-2）。規則 0 / 黑名單對齊改靠回傳後 post_check。
    system = (
        "你是 LINE 群組對話助理咪寶，繁體中文、短句分行、像在群裡聊天。"
        "使用者貼了一張圖，以下是從圖中 OCR 抽到的文字。"
        "請『根據文字的內容』回應，不是描述圖片："
        "是問題就直接回答、是新聞/觀點就給你的判斷、是單據/文件就摘重點並點出要注意的地方。"
        "第一句必須是具體判斷或答案，禁止 echo 複述原文，"
        "禁止『這張圖 / 圖中顯示 / 我看到圖片』這種開頭。不確定就說不確定，不要編造數字或來源。"
    )
    try:
        out = local_chat(text[:1500], system_prompt=system, max_tokens=500)
    except Exception as e:
        logger.warning("_respond_to_ocr_text: local_llm.chat failed: %s", e)
        return None
    reply = out.strip() if out and out.strip() else None
    if not reply:
        return None
    try:
        from vision_common import post_check
        reply = (post_check(reply) or "").strip()
    except Exception as e:
        logger.warning("_respond_to_ocr_text: post_check skipped: %s", e)
    if not reply:
        return None
    _local_llm_down_alerted = False  # 成功＝本機腦袋活著 → reset，下次真的掛掉能再告警
    return reply


def _to_bytes(media) -> Optional[bytes]:
    """Normalize 多型 media 輸入到 bytes（只給 sha256 算），原 media 不動。"""
    if isinstance(media, (bytes, bytearray)):
        return bytes(media)
    if isinstance(media, (str, Path)):
        try:
            with open(media, "rb") as f:
                return f.read()
        except Exception:
            return None
    return None


def _is_cache_quality_reply(reply) -> bool:
    """Phase 1 quality gate：排 OCR-only fallback / 太短 raw desc / 空字串。"""
    if not reply or not reply.strip():
        return False
    s = reply.strip()
    if s.startswith("📷 OCR"):           # L4 OCR-only fallback prefix
        return False
    if len(s) < _MIN_CACHE_REPLY_LEN:    # L3 raw desc 通常 < 200 字
        return False
    return True


def _maybe_lookup_media_cache(
    media,
    group_id: Optional[str],
    media_type: str,
) -> Optional[str]:
    """Cache hit return last_reply + bump_seen；miss / 無 group_id / 極小檔回 None。"""
    if not group_id:
        return None
    image_bytes = _to_bytes(media)
    if not image_bytes or len(image_bytes) < _MIN_CACHE_BYTES:
        return None
    try:
        import memory
        sha = memory.compute_sha256(image_bytes)
        hit = memory.lookup_media_cache(group_id, media_type, sha)
        if hit:
            memory.bump_media_cache_seen(hit["cache_id"])
            logger.info(
                "media_cache HIT group=%s type=%s sha=%s seen=%d",
                group_id[:8], media_type, sha[:8], hit["seen_count"] + 1,
            )
            return hit["last_reply"]
    except Exception as e:
        logger.warning("media_cache lookup failed: %s", e)
    return None


def _maybe_write_media_cache(
    media,
    group_id: Optional[str],
    media_type: str,
    description: Optional[str],
    reply: str,
) -> None:
    """Phase 1：caller 在高品質 layer return 前 call；quality gate 內部過。"""
    if not group_id or not _is_cache_quality_reply(reply):
        return
    image_bytes = _to_bytes(media)
    if not image_bytes or len(image_bytes) < _MIN_CACHE_BYTES:
        return
    try:
        import memory
        sha = memory.compute_sha256(image_bytes)
        memory.insert_media_cache(group_id, media_type, sha, description, reply)
        logger.info(
            "media_cache WRITE group=%s type=%s sha=%s reply_len=%d",
            group_id[:8], media_type, sha[:8], len(reply),
        )
    except Exception as e:
        logger.warning("media_cache write failed: %s", e)


def analyze_image(
    image: str | Path | bytes,
    user_prompt: str = "",
    group_id: str | None = None,
) -> Optional[str]:
    """對圖片產生回應。失敗回 None。

    流程：
      1. OCR 抽文字（如果有 ocr_helper）
      2. 本機 Vision LLM（mlx-vlm Qwen2.5-VL-7B）看圖 + 用 OCR 文字輔助 prompt
      3. 本機失敗 → 至少回 OCR 文字（零雲端 fallback）

    Phase 1（media_cache）：caller 傳 group_id 時走 byte-exact cache dedup
    （group-scoped），命中跳過 v4 7-step pipeline。
    """
    # Phase 1 media_cache lookup
    cached = _maybe_lookup_media_cache(image, group_id, "image")
    if cached:
        return cached

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
        # 沒 vision 描述（多半本機 vision 模型載不進來）。不要只裸吐 OCR
        # （user: 要「根據 OCR 結果回應」，不是只做辨識）：先用本機 LLM 針對 OCR 內容生回應。
        if ocr_text:
            ocr_reply = _respond_to_ocr_text(ocr_text)
            if ocr_reply:
                _maybe_write_media_cache(image, group_id, "image", ocr_text, ocr_reply)
                return ocr_reply
            # 本機 LLM 也載不進來 → 誠實降級 + 一次性告警（不再裸吐 OCR 當答案）。
            # ⚠️ 此降級訊息「絕不」寫入 media_cache：它是暫時性 model-down 狀態，模型恢復後
            #    必須能重答；快取它會在恢復後永遠回放這句（stale-cache bug）。勿在此加 cache write。
            _alert_local_llm_down("vision + 對話模型皆無法載入")
            # 不附 raw OCR 文字：user 要的是「根據內容回應」，裸吐 OCR 正是要避免的行為。
            # 本機腦袋掛掉時就誠實說明、別假裝在回答。
            return (
                "我有看到圖裡有文字，但我本機的 AI 模型現在載不進來，"
                "等恢復我再針對內容仔細回你 🙏"
            )
        return None

    # Layer 2：v4 完整 pipeline（7 步：query expansion + multi-source + full text + critique）
    # 圖片 raw bytes 100% 本機；只把描述文字 + sources 送 Gemini 寫 reply
    import os as _os
    if _os.environ.get("MEDIA_HYBRID_DISABLED") != "1":
        try:
            if _os.environ.get("MEDIA_PIPELINE_V4", "1") != "0":
                wrapped = _v4_news_style_pipeline(desc, ocr_text or "")
            else:
                wrapped = _wrap_with_gemini_news_style(desc, ocr_text or "")
            if wrapped:
                _maybe_write_media_cache(image, group_id, "image", desc, wrapped)
                return wrapped
        except Exception as e:
            logger.warning("hybrid pipeline failed, fallback to raw desc: %s", e)

    # Layer 3：純 vision_llm 描述（不 wrap）
    return desc


def _v4_news_style_pipeline(desc: str, ocr_text: str = "") -> Optional[str]:
    """v4 完整 7 步 pipeline — 比 _wrap_with_gemini_news_style 豐富 3-5x。

    Step 1: vision_describe + OCR（caller 已給）
    Step 2: query expansion（本機 14B 生 6 個多樣 search query）
    Step 3: multi-source aggregate（DDG/GNews/Wiki × N，權威 domain 排序）
    Step 4: top-5 full text fetch（trafilatura 平行）
    Step 5: generate rich reply（Gemini 主 / 14B fallback，新 prompt 強制 ≥5 URL）
    Step 6: grounding verify（grounding_local 4-signal）
    Step 7: self-critique refine（找 hallucinate + 補 missing fact）

    全步驟有 fallback；任一爆 → graceful 退到簡版。
    """
    # ── Step 2: Query expansion ──
    queries = []
    try:
        from finetune_query_expansion import expand_queries
        queries = expand_queries(desc, ocr_text, n=6)
        logger.info("v4 step 2: %d queries → %s", len(queries), queries[:3])
    except Exception as e:
        logger.warning("v4 step 2 expand_queries failed: %s", e)
        # fallback 用單一 query
        queries = [(desc[:80] + " " + ocr_text[:50]).strip()]

    # ── Step 3: Multi-source aggregate ──
    sources = []
    try:
        from source_aggregator import aggregate_sources
        sources = aggregate_sources(queries, total_max=18)
        logger.info(
            "v4 step 3: %d sources（top authority: %s）",
            len(sources),
            [s.get("domain") for s in sources[:3]],
        )
    except Exception as e:
        logger.warning("v4 step 3 aggregate failed: %s", e)
        return _wrap_with_gemini_news_style(desc, ocr_text)  # 退舊版

    # ── Step 4: Top-5 full text fetch ──
    rich_sources = sources
    try:
        from fulltext_fetcher import fetch_top_sources
        rich_sources = fetch_top_sources(sources, top_n=5, max_chars_per=2500)
        full_count = sum(1 for r in rich_sources if r.get("full_text"))
        logger.info("v4 step 4: %d / %d sources fetched full text", full_count, len(rich_sources))
    except Exception as e:
        logger.warning("v4 step 4 fetch_top failed: %s", e)

    # 拼 sources block
    sources_block = "\n\n".join(
        f"[{i+1}] {(r.get('title') or '')[:80]} ({r.get('domain', '?')}, "
        f"權威 {r.get('authority_score', 0)})\n"
        f"     URL: {r.get('url') or ''}\n"
        f"     {(r.get('full_text') or r.get('snippet') or '')[:2000]}"
        for i, r in enumerate(rich_sources[:10])
    )

    # ── Step 5: Generate rich reply ──
    user_msg = (
        f"LINE 群有人貼了一張圖。請寫一段豐富、有具體 fact、引多源的咪寶風回覆。\n\n"
        f"【圖片描述】\n{desc}\n"
        f"【OCR 文字】\n{ocr_text or '(無)'}\n\n"
        f"【相關 sources（{len(rich_sources)} 條，按權威排序，前 5 已 fetch 完整內容）】\n"
        f"{sources_block or '(沒抓到 sources)'}\n\n"
        f"=== 結構（敘述體不要章節 header）===\n"
        f"1. 第一段：直接給整合判斷（規則 0，第一句具體 take，含至少 1 個關鍵數字 / 名詞）\n"
        f"2. 第二段：正方/支持方在說什麼，引 [n] sources，含人名/機構/日期\n"
        f"3. 第三段：反方/質疑方在說什麼，引 [n] sources，含具體質疑點\n"
        f"4. 第四段：歷史脈絡 / 背景 — 為什麼這事重要 / 過去有什麼類似案例\n"
        f"5. 第五段：整合 — 你綜合多源後的判斷（含具體數字、機制、為什麼）\n"
        f"6. 第六段：sources 矛盾分析 — 哪些 source 之間有不同說法（如有）\n"
        f"7. 第七段：actionable 建議或警告（1-2 句，user 看完該做什麼）\n"
        f"8. 最後段：來源 — 列至少 5 條，格式 `機構名 https://URL`，每條一行（從上面 sources 直接複製 URL，不編造、不 short URL）\n\n"
        f"=== 硬性規則 ===\n"
        f"- 必須引用至少 5 個 sources（[1][2]... 對應上面）\n"
        f"- 來源段必須 5+ 條真實 URL（從上面 sources 區塊原樣複製）\n"
        f"- 必須含具體：人名、日期、數字、機構（至少 5 個）\n"
        f"- 不要 formal section header（同意/反對/判斷依據/結論）\n"
        f"- 不要「希望對您有幫助 / 以上僅供參考」結尾\n"
        f"- 整體 400-700 字\n"
        f"- 忠實 follow sources（沒提的不要憑記憶補；矛盾要標出）\n\n"
        f"直接給回覆，不要前綴。"
    )

    # 圖片 100% 本機 policy（user 2026-05-09）：跳過 Gemini，直接走本機 14B
    # 圖片內容 / 描述都不送雲端，包括 Step 5 reply generation
    reply = None
    logger.info("v4 step 5 圖片走本機 14B（skip Gemini per image policy）")
    try:
        from local_llm import chat as local_chat
        meibao_system = (
            "你是 LINE 群組對話助理咪寶。風格要求：繁體中文、第一句具體判斷、"
            "敘述體不要 section header、含具體數字/人名/日期/機構、引用至少 5 個 sources [n]、"
            "來源段含真實 URL（5+ 條，從上面 sources 區塊原樣複製，不替換、不編造）、整體 400-700 字。"
            "禁止「同意/反對/判斷依據/結論」formal headers、禁止「希望對您有幫助」結尾。"
        )
        reply = local_chat(user_msg, system_prompt=meibao_system, max_tokens=1200)
        reply = reply.strip() if reply else None
    except Exception as e:
        logger.warning("v4 step 5 local_llm failed: %s", e)
        return None

    if not reply:
        return None

    # ── Step 6: Grounding verify ──
    try:
        import grounding_local
        source_texts = [
            (r.get("full_text") or r.get("snippet") or "") for r in rich_sources[:5]
        ]
        score = grounding_local.score_response(reply, source_texts)
        avg = score.get("score_avg", 1.0) if score else 1.0
        logger.info("v4 step 6 grounding score: %.2f", avg)
    except Exception as e:
        logger.info("v4 step 6 grounding skip: %s", e)

    # ── Step 7: Self-critique refine（圖片 100% 本機 → critique/refine 也用 14B，不打 Gemini）──
    import os as _os2
    _os2.environ["SELF_CRITIQUE_FORCE_LOCAL"] = "1"  # 給 self_critique 用的 hint
    try:
        from self_critique import critique_reply, refine_reply
        sources_for_critique = [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "text": r.get("full_text") or r.get("snippet") or "",
            }
            for r in rich_sources[:5]
        ]
        critique = critique_reply(reply, sources_for_critique)
        n_contradicted = sum(
            1 for c in critique.get("claims", []) if c.get("verdict") == "contradicted"
        )
        n_missing = len(critique.get("missing_facts", []))
        logger.info(
            "v4 step 7 critique: %d contradicted, %d missing facts",
            n_contradicted, n_missing,
        )
        if n_contradicted > 0 or n_missing >= 2:
            refined = refine_reply(reply, critique, sources_for_critique)
            if refined and refined.strip():
                logger.info("v4 step 7 refine applied")
                return refined.strip()
    except Exception as e:
        logger.info("v4 step 7 critique/refine skip: %s", e)

    return reply


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
    group_id: str | None = None,
) -> Optional[str]:
    """對影片產生回應。失敗回 None。

    流程：
      1. 抽 keyframes（max 6）
      2. 本機多圖 vision LLM 看
      3. 本機失敗 → None（沉默）

    Phase 1（media_cache）：caller 傳 group_id 時走 byte-exact cache dedup。
    """
    # Phase 1 media_cache lookup
    cached = _maybe_lookup_media_cache(video, group_id, "video")
    if cached:
        return cached

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

    if out and out.strip():
        _maybe_write_media_cache(video, group_id, "video", None, out)
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
