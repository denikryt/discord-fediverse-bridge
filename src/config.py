from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    discord_token: str = Field(alias="DISCORD_TOKEN")
    discord_forum_channel_id: int = Field(alias="DISCORD_FORUM_CHANNEL_ID")
    lemmy_base_url: HttpUrl = Field(alias="LEMMY_BASE_URL")
    lemmy_username_or_email: str = Field(alias="LEMMY_USERNAME_OR_EMAIL")
    lemmy_password: str = Field(alias="LEMMY_PASSWORD")
    lemmy_community_name: str = Field(alias="LEMMY_COMMUNITY_NAME")

    database_url: str = Field(default="sqlite:///./bridge.db", alias="DATABASE_URL")
    poll_interval_seconds: int = Field(default=5, alias="POLL_INTERVAL_SECONDS")
    bridge_display_prefix: str = Field(default="[bridge]", alias="BRIDGE_DISPLAY_PREFIX")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def normalized_lemmy_base_url(self) -> str:
        return str(self.lemmy_base_url).rstrip("/")
