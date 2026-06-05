"""Configuration loaded from environment variables.

Railway injects these via the dashboard; locally use a .env file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


# Resy's public web app has used this same API key for years. It's not a
# secret — it's baked into their JS bundle. Override via env if it ever rotates.
DEFAULT_RESY_API_KEY = "VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"


@dataclass(frozen=True)
class Settings:
    discord_token: str
    discord_guild_id: int | None  # if set, slash commands sync to this guild only (instant)
    resy_api_key: str
    data_path: Path
    default_poll_seconds: int
    user_agent: str
    alert_cooldown_minutes: int
    http_timeout_seconds: float

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("DISCORD_TOKEN", "").strip()
        if not token:
            raise SystemExit("DISCORD_TOKEN is required")

        guild_raw = os.environ.get("DISCORD_GUILD_ID", "").strip()
        guild_id = int(guild_raw) if guild_raw else None

        # Railway volume mounts at /data by default; fall back to local ./data
        data_path = Path(os.environ.get("DATA_PATH", "/data/store.json"))
        data_path.parent.mkdir(parents=True, exist_ok=True)

        return cls(
            discord_token=token,
            discord_guild_id=guild_id,
            resy_api_key=os.environ.get("RESY_API_KEY", DEFAULT_RESY_API_KEY),
            data_path=data_path,
            default_poll_seconds=int(os.environ.get("DEFAULT_POLL_SECONDS", "30")),
            user_agent=os.environ.get(
                "USER_AGENT",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36",
            ),
            alert_cooldown_minutes=int(os.environ.get("ALERT_COOLDOWN_MINUTES", "240")),
            http_timeout_seconds=float(os.environ.get("HTTP_TIMEOUT_SECONDS", "15")),
        )
