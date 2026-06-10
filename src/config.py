"""Environment-backed settings for the Python bridge process."""

from pydantic import Field, HttpUrl, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Settings centralize the bridge contract with its environment so both the
    # bot runtime and the internal HTTP API read the same values.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
    )

    discord_token: str = Field(alias="DISCORD_TOKEN")

    database_url: str = Field(default="sqlite:///./bridge.db", alias="DATABASE_URL")
    public_base_url: HttpUrl = Field(alias="PUBLIC_BASE_URL")
    fedify_gateway_url: HttpUrl = Field(default="http://127.0.0.1:3000", alias="BRIDGE_GATEWAY_URL")
    internal_http_host: str = Field(default="127.0.0.1", alias="BRIDGE_BIND_HOST")
    internal_http_port: int = Field(default=8080, alias="BRIDGE_BIND_PORT")
    discord_oauth_client_id: str = Field(default="", alias="DISCORD_OAUTH_CLIENT_ID")
    discord_oauth_client_secret: str = Field(default="", alias="DISCORD_OAUTH_CLIENT_SECRET")
    discord_oauth_redirect_uri: HttpUrl | None = Field(default=None, alias="DISCORD_OAUTH_REDIRECT_URI")
    registration_session_cookie_name: str = Field(default="bridge_registration_session", alias="REGISTRATION_SESSION_COOKIE_NAME")
    registration_session_ttl_seconds: int = Field(default=3600, alias="REGISTRATION_SESSION_TTL_SECONDS")
    fedify_shared_secret: str = Field(alias="FEDIFY_SHARED_SECRET")
    fedify_actor_identifier: str = Field(default="bridge", alias="FEDIFY_ACTOR_IDENTIFIER")
    # Legacy bridge JWK values are accepted only for one-time database import.
    fedify_bridge_private_key_jwk_json: str | None = Field(
        default=None, alias="FEDIFY_BRIDGE_PRIVATE_KEY_JWK_JSON", repr=False
    )
    fedify_bridge_public_key_jwk_json: str | None = Field(
        default=None, alias="FEDIFY_BRIDGE_PUBLIC_KEY_JWK_JSON"
    )
    bridge_display_prefix: str = Field(default="[bridge]", alias="BRIDGE_DISPLAY_PREFIX")
    dashboard_page_title: str = Field(
        default="Discord/Fediverse Bridge Instance",
        alias="DASHBOARD_PAGE_TITLE",
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    # Bootstrap policy remains immutable for the process lifetime. Dynamic
    # entries are persisted separately and merged by BridgePolicyService.
    federation_allowlist: list[str] = Field(default_factory=list, alias="FEDERATION_ALLOWLIST")
    federation_blocklist: list[str] = Field(default_factory=list, alias="FEDERATION_BLOCKLIST")
    discord_guild_allowlist: list[str] = Field(default_factory=list, alias="DISCORD_GUILD_ALLOWLIST")
    discord_guild_blocklist: list[str] = Field(default_factory=list, alias="DISCORD_GUILD_BLOCKLIST")
    bridge_super_admin_user_ids: list[str] = Field(alias="BRIDGE_SUPER_ADMIN_USER_IDS")

    @field_validator("federation_allowlist", "federation_blocklist", mode="before")
    @classmethod
    def _split_allowlist(cls, v: object) -> list[str]:
        # Accept comma-separated env values:
        # Host policy lists are comma-separated and normalized later by the
        # runtime policy service so startup parsing remains transport-neutral.
        if v is None or v == "":
            return []

        if isinstance(v, str):
            return [entry.strip() for entry in v.split(",") if entry.strip()]

        if isinstance(v, int):
            return [str(v)]

        return [str(entry).strip() for entry in v if str(entry).strip()]


    @field_validator("discord_guild_allowlist", "discord_guild_blocklist", "bridge_super_admin_user_ids", mode="before")
    @classmethod
    def _split_discord_guild_allowlist(cls, v: object) -> list[str]:
        """Parse and validate comma-separated Discord guild IDs.

        Discord snowflakes are opaque string identifiers in the rest of the
        command layer. Strict decimal validation makes deployment mistakes fail
        at startup instead of silently blocking every guild command.
        """
        if v is None or v == "":
            return []

        if isinstance(v, str):
            entries = [entry.strip() for entry in v.split(",") if entry.strip()]
        elif isinstance(v, int):
            entries = [str(v)]
        else:
            entries = [str(entry).strip() for entry in v if str(entry).strip()]

        invalid = [entry for entry in entries if not entry.isdecimal()]
        if invalid:
            raise ValueError(
                "Discord guild and super-admin policy lists must contain comma-separated decimal Discord IDs"
            )
        return entries

    @model_validator(mode="after")
    def _require_bootstrap_super_admin(self) -> "Settings":
        """Fail startup when no immutable bridge administrator is configured."""
        if not self.bridge_super_admin_user_ids:
            raise ValueError("BRIDGE_SUPER_ADMIN_USER_IDS must contain at least one Discord user ID")
        return self

    @property
    def normalized_public_base_url(self) -> str:
        """Return the canonical public origin without a trailing slash."""
        return str(self.public_base_url).rstrip("/")

    @property
    def normalized_public_bridge_base_url(self) -> str:
        return self.normalized_public_base_url

    @property
    def normalized_fedify_origin(self) -> str:
        return self.normalized_public_base_url

    @property
    def public_bridge_base_url(self) -> str:
        return self.normalized_public_base_url

    @property
    def fedify_origin(self) -> str:
        return self.normalized_public_base_url

    @property
    def resolved_discord_oauth_redirect_uri(self) -> str:
        """Return the explicit OAuth callback or derive it from the public origin."""
        if self.discord_oauth_redirect_uri is not None:
            return str(self.discord_oauth_redirect_uri)
        return f"{self.normalized_public_base_url}/auth/discord/callback"
