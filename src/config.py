from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Settings centralize the bridge contract with its environment so both the
    # bot runtime and the internal HTTP API read the same values.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    discord_token: str = Field(alias="DISCORD_TOKEN")
    lemmy_base_url: HttpUrl = Field(alias="LEMMY_BASE_URL")
    lemmy_username_or_email: str = Field(alias="LEMMY_USERNAME_OR_EMAIL")
    lemmy_password: str = Field(alias="LEMMY_PASSWORD")

    # Legacy single-pair config — if both are set, a default subscription is
    # created in the DB on first startup so existing deployments keep working.
    discord_forum_channel_id: int | None = Field(default=None, alias="DISCORD_FORUM_CHANNEL_ID")
    lemmy_community_name: str | None = Field(default=None, alias="LEMMY_COMMUNITY_NAME")
    lemmy_community_actor_id: str | None = Field(default=None, alias="LEMMY_COMMUNITY_ACTOR_ID")

    database_url: str = Field(default="sqlite:///./bridge.db", alias="DATABASE_URL")
    fedify_gateway_url: HttpUrl = Field(default="http://127.0.0.1:3000", alias="FEDIFY_GATEWAY_URL")
    internal_http_host: str = Field(default="127.0.0.1", alias="INTERNAL_HTTP_HOST")
    internal_http_port: int = Field(default=8080, alias="INTERNAL_HTTP_PORT")
    fedify_shared_secret: str = Field(alias="FEDIFY_SHARED_SECRET")
    bridge_display_prefix: str = Field(default="[bridge]", alias="BRIDGE_DISPLAY_PREFIX")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def normalized_lemmy_base_url(self) -> str:
        # Keep one canonical base URL shape so API path joins do not depend on
        # whether the env var had a trailing slash.
        return str(self.lemmy_base_url).rstrip("/")
