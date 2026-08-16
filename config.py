"""
Env loader.

部署目標：本機 Mac + Cloudflare quick tunnel（免帳號、免付費）
儲存：本機 SQLite (line_bot.db) — 取代 Upstash Redis，免外部服務

群組白名單（2026-05-27 multi-group 加入媽媽個人群後）：
- ALLOWED_GROUP_IDS（複數，逗號分隔）= webhook gate 白名單；空 list = unlock mode 接受所有群（log only）
- ALLOWED_GROUP_ID（單數，legacy）= 舊單群設定；若 ALLOWED_GROUP_IDS 未設則自動 seed 進 list
- FAMILY_GROUP_ID（顯式）= 「家族主群」識別，給 family-only push script 用；不從 ALLOWED_GROUP_IDS[0]
  positional 衍生，避免 reorder 環境變數靜默把家族專屬內容推到 mom 群
"""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_csv(raw: str) -> list[str]:
    """Comma-CSV → list[str] with strip + dedup + drop-empty."""
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for token in raw.split(","):
        gid = token.strip()
        if gid and gid not in seen:
            seen.add(gid)
            out.append(gid)
    return out


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

    # ── Claude（optional primary cloud provider）───────────────────────────
    # Claude is attempted before Gemini for main replies when a key is set.
    # Multiple aliases keep local `.env` and the lifecycle agent convention
    # compatible without duplicating secrets.
    claude_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "claude_api_key",
            "ANTHROPIC_API_KEY",
            "CLAUDE_API_KEY",
            "AGENT_CLAUDE_API_KEY",
        ),
    )
    claude_model: str = Field(
        default="claude-sonnet-4-5-20250929",
        validation_alias=AliasChoices(
            "claude_model",
            "ANTHROPIC_MODEL",
            "CLAUDE_MODEL",
            "AGENT_CLAUDE_MODEL",
        ),
    )
    # A quota/rate-limit gate is persisted across restarts. Keep this
    # configurable because billing reset and rate-limit windows differ.
    claude_quota_cooldown_sec: int = Field(
        default=3600,
        validation_alias="CLAUDE_QUOTA_COOLDOWN_SEC",
    )
    claude_request_timeout_sec: int = Field(
        default=20,
        validation_alias="CLAUDE_REQUEST_TIMEOUT_SEC",
    )
    claude_cli_timeout_sec: int = Field(
        default=60,
        validation_alias="CLAUDE_CLI_TIMEOUT_SEC",
    )
    # Optional account-session route. The client auto-enables this after an
    # API billing-balance error, but the flag also supports CLI-only setups.
    claude_use_cli: bool = Field(
        default=False,
        validation_alias="CLAUDE_USE_CLI",
    )

    # ── SQLite（本地持久化檔案）───────────────────────────────────────────────
    sqlite_path: str = "line_bot.db"

    @field_validator("sqlite_path", mode="before")
    @classmethod
    def _resolve_sqlite_path(cls, value: str) -> str:
        """Anchor relative SQLite paths to the bot package, not process cwd.

        launchd jobs and webhook workers can have different working directories;
        a relative path therefore risks opening a second database or failing to
        open one at all. Explicit absolute paths remain unchanged.
        """
        raw = str(value or "line_bot.db").strip()
        path = Path(raw).expanduser()
        if path.is_absolute():
            return str(path)
        return str((Path(__file__).resolve().parent / path).resolve())

    # ── 群組白名單 ─────────────────────────────────────────────────────────
    # allowed_group_id（單數，legacy real field）：保留為 mutable 欄位，現有 5 個
    # test file 直接賦值（conftest.py / test_handlers.py / test_silent_drop.py /
    # test_regression.py / test_extra_coverage.py）才不會壞。空字串 = 尚未鎖定。
    allowed_group_id: str = ""

    # allowed_group_ids_raw（單一 csv str）：env `ALLOWED_GROUP_IDS` raw 值；real list
    # 走 `allowed_group_ids` @computed_field 衍生。**為何拆兩個**：pydantic-settings 2.6
    # 對 `list[str]` field 強制 JSON decode（看到 `Cxxx` 而非 `["Cxxx"]` 就 crash），
    # 沒有 NoDecode marker（2.7+ 才有）。Workaround：raw 用 str 接 env，computed_field
    # 衍生 list。
    allowed_group_ids_raw: str = Field(default="", validation_alias="ALLOWED_GROUP_IDS")

    # family_group_id：顯式「家族主群」識別。給 family-only push script 讀
    # （feedback_push / family_interest / weekly_review / monthly_highlight
    # / process_feedback / announce_finance_view / line_bot_update_push / weekly_summary
    # / weekly_retro），避免 positional `[0]` anti-pattern。空字串時自動 fall back 到
    # legacy `ALLOWED_GROUP_ID`（也不會 fall back 到 allowed_group_ids[0] — reviewer 兩位
    # 都警告 positional fragility）。
    family_group_id: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def allowed_group_ids(self) -> list[str]:
        """webhook gate 真實用的白名單。空 list = unlock mode（接受所有群，僅 log group_id）。
        優先用 `ALLOWED_GROUP_IDS`（csv）解析；若空且 legacy `ALLOWED_GROUP_ID` 有值，
        自動 seed 進 list 維持 backward compat。"""
        if self.allowed_group_ids_raw:
            return _parse_csv(self.allowed_group_ids_raw)
        if self.allowed_group_id:
            return [self.allowed_group_id]
        return []

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

    # ── Feature flags ────────────────────────────────────────────────────────
    # Phase 2B.4: route chat() RAG flow through rag_graph (LangGraph).
    # Default OFF — flag ON must produce byte-identical output to OFF
    # (parity contract per rag_graph.py:7-8). Flip via `.env`
    # USE_RAG_GRAPH=true + full uvicorn restart (pydantic-settings reads
    # `.env` at import; SIGHUP won't reload).
    use_rag_graph: bool = False

    # Group-local organic correction canonicalization.  The projection is
    # additive; setting this false restores the legacy raw recent-10 prompt
    # without deleting audit or canonical data.
    correction_canonical_memory_enabled: bool = True

    # ── Phase 2B.6 hardening validators ──────────────────────────────────────
    # field-level: every Gemini model name must look like "gemini-..." (catches
    # fat-fingered env values). model-level: gemini_model and gemini_light_model
    # MUST differ — else rag_graph allowlist collapses to a 1-element set AND
    # the 503/429 lite fallback gate (`model != settings.gemini_light_model`)
    # never fires. Misconfigured `.env` will fail at import — every cron job
    # (daily_briefing_discord, feedback_push, etc.) WILL crash on
    # startup; this is fail-fast hardening, not subtle behaviour change.

    @field_validator("gemini_model", "gemini_light_model")
    @classmethod
    def _validate_gemini_model_name(cls, v: str) -> str:
        # `.strip()` neutralizes trailing-space typos (GP2 NIT) so we don't
        # silently accept e.g. "gemini-2.5-flash " which passes prefix check
        # but fails the actual SDK call.
        v = v.strip()
        if not v.startswith("gemini-"):
            raise ValueError(
                f"Gemini model name must start with 'gemini-' (got {v!r}); "
                "otherwise rag_graph allowlist would silently bypass on lite fallback."
            )
        return v

    @model_validator(mode="after")
    def _models_distinct(self):
        if self.gemini_model == self.gemini_light_model:
            raise ValueError(
                "gemini_model and gemini_light_model must differ "
                f"(both are {self.gemini_model!r}) — otherwise rag_graph "
                "allowlist collapses to 1 element AND 503/429 fallback never fires."
            )
        return self

    @model_validator(mode="after")
    def _seed_family_from_legacy(self):
        # 若 family_group_id 未明示，fallback 到 allowed_group_id（單數 legacy 即 family）。
        # **不** fallback 到 allowed_group_ids[0]：reviewer 兩位都警告 positional fragility
        # ——env reorder = 家族專屬內容靜默推到 mom 群。要 multi-group 時 user 必須顯式設
        # FAMILY_GROUP_ID。
        if not self.family_group_id and self.allowed_group_id:
            self.family_group_id = self.allowed_group_id
        return self


settings = Settings()  # type: ignore[call-arg]
