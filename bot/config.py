from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from bot.models.deadlock import DeadlockGameMode, DeadlockServerRegion


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True)

    bot_token: str
    guild_id: int
    command_prefix: str = "!"
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db_name: str = "bebop"
    log_level: str = "INFO"

    # Queue settings
    queue_size: int = 12  # Deadlock games are 12 players, so this should always be 12 unless you are testing something.
    queue_channel_id: int | None = None
    commands_channel_id: int | None = None
    admin_role_name: str = "PUG Manager"

    # Deadlock API settings
    deadlock_api_base_url: str = "https://api.deadlock-api.com"
    deadlock_api_key: str | None = None
    deadlock_api_timeout_seconds: float = 10.0
    deadlock_custom_game_mode: DeadlockGameMode = DeadlockGameMode.NORMAL
    deadlock_custom_server_region: DeadlockServerRegion | None = None
    deadlock_custom_disable_auto_ready: bool = False
    deadlock_custom_is_publicly_visible: bool = True
    deadlock_custom_min_roster_size: int | None = None

    # Deadlock callback settings
    deadlock_callback_enabled: bool = False
    deadlock_callback_public_base_url: str | None = None
    deadlock_callback_bind_host: str = "0.0.0.0"
    deadlock_callback_bind_port: int = 8080
    deadlock_callback_path_prefix: str = "/callbacks/deadlock"


settings = Settings()
