from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Settings centralize the bridge contract with its environment so both the
    # bot runtime and the internal HTTP API read the same values.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    discord_token: str = Field(alias="DISCORD_TOKEN")
    lemmy_base_url: HttpUrl = Field(alias="LEMMY_BASE_URL")

    database_url: str = Field(default="sqlite:///./bridge.db", alias="DATABASE_URL")
    fedify_gateway_url: HttpUrl = Field(default="http://127.0.0.1:3000", alias="FEDIFY_GATEWAY_URL")
    internal_http_host: str = Field(default="127.0.0.1", alias="INTERNAL_HTTP_HOST")
    internal_http_port: int = Field(default=8080, alias="INTERNAL_HTTP_PORT")
    public_bridge_base_url: HttpUrl = Field(default="http://127.0.0.1:8080", alias="PUBLIC_BRIDGE_BASE_URL")
    discord_oauth_client_id: str = Field(default="", alias="DISCORD_OAUTH_CLIENT_ID")
    discord_oauth_client_secret: str = Field(default="", alias="DISCORD_OAUTH_CLIENT_SECRET")
    discord_oauth_redirect_uri: HttpUrl = Field(default="http://127.0.0.1:8080/auth/discord/callback", alias="DISCORD_OAUTH_REDIRECT_URI")
    registration_session_cookie_name: str = Field(default="bridge_registration_session", alias="REGISTRATION_SESSION_COOKIE_NAME")
    registration_session_ttl_seconds: int = Field(default=3600, alias="REGISTRATION_SESSION_TTL_SECONDS")
    fedify_shared_secret: str = Field(alias="FEDIFY_SHARED_SECRET")
    bridge_display_prefix: str = Field(default="[bridge]", alias="BRIDGE_DISPLAY_PREFIX")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def normalized_lemmy_base_url(self) -> str:
        # Keep one canonical base URL shape so API path joins do not depend on
        # whether the env var had a trailing slash.
        return str(self.lemmy_base_url).rstrip("/")

    @property
    def normalized_public_bridge_base_url(self) -> str:
        # Registration and actor URLs must use one stable public origin so the
        # bot, FastAPI backend, and Fedify gateway all advertise the same URLs.
        return str(self.public_bridge_base_url).rstrip("/")
