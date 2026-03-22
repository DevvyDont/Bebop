from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    bot_token: str
    guild_id: int
    command_prefix: str = "!"
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db_name: str = "bebop"
    log_level: str = "INFO"


settings = Settings()
