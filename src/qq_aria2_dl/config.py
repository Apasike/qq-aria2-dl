"""配置加载与校验：从 .env 读取 Settings。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class GroupConfig(BaseModel):
    """Per-group settings. Whitelisting is no longer per-group; download
    permission is decided by ``download_allowed_users``. This config only
    controls whether the group is processed and whether auto-download is on."""

    enabled: bool = True
    auto_download: bool = False
    require_at: bool = False
    allowed_users: list[str] = Field(default_factory=list)  # kept for compatibility, unused
    admins: list[str] = Field(default_factory=list)
    max_urls_per_message: int = 3

    @field_validator("allowed_users", "admins", mode="before")
    @classmethod
    def _stringify_list(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            v = v.strip()
            if v == "*":
                return ["*"]
            return [x.strip() for x in v.split(",") if x.strip()]
        return [str(x) for x in v]

    @field_validator("max_urls_per_message")
    @classmethod
    def _positive_urls(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_urls_per_message must be >= 1")
        return v


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # app
    app_host: str = "127.0.0.1"
    app_port: int = 8080
    log_level: str = "INFO"

    # onebot
    onebot_path: str = "/onebot/v11/ws"
    onebot_token: str = "change-me"
    onebot_heartbeat_timeout_sec: float = 90.0
    onebot_send_timeout_sec: float = 10.0

    # aria2
    aria2_rpc_url: str = "http://127.0.0.1:6800/jsonrpc"
    aria2_secret: str = "change-me"
    aria2_poll_interval_sec: float = 2.0
    aria2_download_dir: str = ""  # empty => use aria2 default dir
    aria2_max_concurrent: int = 3
    aria2_split: int = 1
    aria2_max_connection_per_server: int = 1

    # progress
    progress_cache_ttl_sec: float = 5.0
    progress_query_cooldown_sec: float = 3.0
    progress_rpc_cache_sec: float = 2.0
    progress_max_tasks_per_query: int = 5

    # storage
    storage_db_path: str = "data/bot.db"
    storage_task_retention_days: int = 30

    # dedupe
    dedupe_ttl_sec: int = 600
    dedupe_scope: str = "chat"

    # disk
    disk_min_free_bytes: int = 5 * 1024**3
    disk_min_free_percent: float = 10.0
    disk_critical_free_bytes: int = 2 * 1024**3
    disk_unknown_size_reserve_bytes: int = 512 * 1024**2
    disk_head_timeout_sec: float = 5.0
    disk_auto_resume: bool = False

    # rate limit (per 10 minutes)
    rate_limit_per_user_per_10min: int = 3
    rate_limit_per_group_per_10min: int = 10

    # download whitelist: QQ numbers allowed to download via commands.
    # Seeded into SQLite on startup; admins can add/remove at runtime with /wl.
    download_allowed_users: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # groups: JSON map group_id -> GroupConfig. Only used for auto_download /
    # require_at / enabled switches, not for download permission.
    groups: Annotated[dict[str, GroupConfig], NoDecode] = Field(default_factory=dict)

    # global admins (comma separated)
    admin_users: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # only these groups may trigger downloads (empty = no group restriction)
    allowed_groups: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # url safety (comma separated)
    allowed_schemes: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http", "https"]
    )
    max_url_length: int = 8192
    download_user_agent: str = ""  # empty => let aria2 use its own user-agent
    download_referer: str = ""

    @field_validator("download_allowed_users", "admin_users", "allowed_groups", "allowed_schemes", mode="before")
    @classmethod
    def _parse_str_list(cls, v: Any) -> Any:
        if v is None:
            return []
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    @field_validator("groups", mode="before")
    @classmethod
    def _parse_groups(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return {}
            try:
                return json.loads(v)
            except json.JSONDecodeError as exc:  # pragma: no cover - config error path
                raise ValueError(f"GROUPS must be a JSON object: {exc}") from exc
        return v

    @field_validator("dedupe_scope")
    @classmethod
    def _check_scope(cls, v: str) -> str:
        if v not in ("chat", "global", "user"):
            raise ValueError("DEDUPE_SCOPE must be one of: chat, global, user")
        return v

    def group(self, group_id: int | str) -> GroupConfig | None:
        return self.groups.get(str(group_id))

    def is_global_admin(self, user_id: int | str) -> bool:
        u = str(user_id)
        return "*" in self.admin_users or u in self.admin_users

    def resolve_paths(self) -> None:
        """Only resolve local paths.

        ``aria2_download_dir`` is passed to aria2 **verbatim** because aria2 may
        run on another machine; resolving it here would rewrite a remote Linux
        path into a local Windows path.
        """
        db_path = Path(self.storage_db_path).resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_db_path = str(db_path)


def get_settings() -> Settings:
    settings = Settings()
    settings.resolve_paths()
    return settings