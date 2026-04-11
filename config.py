"""
Env loader.

部署目標：Fly.io（shared-cpu-1x 256 MB 免費 tier）
Redis：Upstash Free Tier（REDIS_URL 走 TLS，格式 rediss://default:pass@host:port）

ALLOWED_GROUP_ID 為空字串時 = 尚未鎖定群組，bot 會把收到的 source.groupId
寫到 stdout，使用者從 `flyctl logs` 抓出來填進 fly secrets 再重啟鎖定。
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── LINE Messaging API ────────────────────────────────────────────────────
    line_channel_secret: str
    line_channel_access_token: str

    # ── Gemini ────────────────────────────────────────────────────────────────
    gemini_api_key: str
    gemini_model: str = "gemini-2.0-flash"

    # ── Redis（Upstash Free Tier，rediss:// 是 TLS 版 Redis）─────────────────
    # Upstash console 直接複製貼上，格式：rediss://default:<pwd>@<host>:6379
    redis_url: str

    # ── 綁定單一群組（Q6=B）─────────────────────────────────────────────────
    # 空字串 = 尚未鎖定，收到訊息時只會 log group id，不會呼叫 LLM
    allowed_group_id: str = ""

    # ── Bot 行為 ──────────────────────────────────────────────────────────────
    # 對話 context 保留幾輪（user + bot 各算一輪）
    context_rounds: int = 15
    # 每聊幾輪自動抽取一次長期記憶
    fact_extract_every: int = 10
    # 最多注入 prompt 的事實數量
    max_facts_in_prompt: int = 20


settings = Settings()  # type: ignore[call-arg]
