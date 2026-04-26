"""
Env loader.

部署目標：本機 Mac + Cloudflare quick tunnel（免帳號、免付費）
儲存：本機 SQLite (line_bot.db) — 取代 Upstash Redis，免外部服務

ALLOWED_GROUP_ID 為空字串時 = 尚未鎖定群組，bot 會把收到的 source.groupId
寫到 stdout，使用者從 log 抓出來填進 .env 再重啟鎖定。
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ── LINE Messaging API ────────────────────────────────────────────────────
    line_channel_secret: str
    line_channel_access_token: str

    # ── Gemini ────────────────────────────────────────────────────────────────
    gemini_api_key: str
    # 主 chat（回給使用者的那次，會用到 thinking + tools + multimodal）
    gemini_model: str = "gemini-2.0-flash"
    # 「輕活」模型（classifier / fact 抽取 / Layer 2 規則生成）
    # 預設 flash-lite，免費額度 1000/天，跟 flash 完全獨立不互相吃
    gemini_light_model: str = "gemini-2.5-flash-lite"

    # ── SQLite（本地持久化檔案）───────────────────────────────────────────────
    sqlite_path: str = "line_bot.db"

    # ── 綁定單一群組（Q6=B）─────────────────────────────────────────────────
    # 空字串 = 尚未鎖定，收到訊息時只會 log group id，不會呼叫 LLM
    allowed_group_id: str = ""

    # ── Bot 行為 ──────────────────────────────────────────────────────────────
    # 對話 context 保留幾輪（user + bot 各算一輪）
    context_rounds: int = 6
    # 每聊幾輪自動抽取一次長期記憶
    fact_extract_every: int = 10
    # 最多注入 prompt 的事實數量
    max_facts_in_prompt: int = 20

    # ── Mute 開關 ─────────────────────────────────────────────────────────────
    # True = 不對 LINE 送出任何回覆（webhook 照收、Gemini 照跑、log 照寫，只是不 push）
    # 預設靜音；修 bug 完成後在 .env 加 BOT_MUTED=false 並 restart uvicorn 解除
    bot_muted: bool = True


settings = Settings()  # type: ignore[call-arg]
