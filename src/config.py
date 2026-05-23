"""Environment-backed settings for the Python bridge process."""

from pydantic import Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Settings centralize the bridge contract with its environment so both the
    # bot runtime and the internal HTTP API read the same values.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    discord_token: str = Field(alias="DISCORD_TOKEN")

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
    fedify_origin: HttpUrl = Field(default="http://127.0.0.1:3000", alias="FEDIFY_ORIGIN")
    bridge_display_prefix: str = Field(default="[bridge]", alias="BRIDGE_DISPLAY_PREFIX")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    # Comma-separated list of allowed Lemmy instance hostnames (e.g. "lemmy.world,beehaw.org").
    # Empty means all instances are allowed (open federation).
    federation_allowlist: list[str] = Field(default_factory=list, alias="FEDERATION_ALLOWLIST")
    # Comma-separated Discord user IDs allowed to create local communities.
    # The bridge keeps this operator list explicit because creating a federated
    # local community is more privileged than subscribing a channel.
    local_community_operator_allowlist: list[str] = Field(
        default_factory=list,
        alias="LOCAL_COMMUNITY_OPERATOR_ALLOWLIST",
    )

    @field_validator("federation_allowlist", "local_community_operator_allowlist", mode="before")
    @classmethod
    def _split_allowlist(cls, v: object) -> list[str]:
        # Accept comma-separated env values:
        #   FEDERATION_ALLOWLIST=lemmy.world,beehaw.org
        #   LOCAL_COMMUNITY_OPERATOR_ALLOWLIST=123456789012345678,987654321098765432
        #
        # Also tolerate pydantic-settings decoding a bare numeric env value as int:
        #   LOCAL_COMMUNITY_OPERATOR_ALLOWLIST=123456789012345678
        if v is None or v == "":
            return []

        if isinstance(v, str):
            return [entry.strip() for entry in v.split(",") if entry.strip()]

        if isinstance(v, int):
            return [str(v)]

        return [str(entry).strip() for entry in v if str(entry).strip()]

    @property
    def normalized_public_bridge_base_url(self) -> str:
        return str(self.public_bridge_base_url).rstrip("/")

    @property
    def normalized_fedify_origin(self) -> str:
        return str(self.fedify_origin).rstrip("/")
