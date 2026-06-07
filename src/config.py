"""Environment-backed settings for the Python bridge process."""

from pydantic import Field, HttpUrl, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Settings centralize the bridge contract with its environment so both the
    # bot runtime and the internal HTTP API read the same values.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    discord_token: str = Field(alias="DISCORD_TOKEN")

    database_url: str = Field(default="sqlite:///./bridge.db", alias="DATABASE_URL")
    # PUBLIC_BASE_URL is the single operator-facing public origin. The legacy
    # values remain accepted temporarily so existing deployments can migrate
    # without changing federation identity during the upgrade.
    public_base_url: HttpUrl | None = Field(default=None, alias="PUBLIC_BASE_URL")
    legacy_public_bridge_base_url: HttpUrl | None = Field(
        default=None, alias="PUBLIC_BRIDGE_BASE_URL", exclude=True
    )
    legacy_fedify_origin: HttpUrl | None = Field(default=None, alias="FEDIFY_ORIGIN", exclude=True)
    fedify_gateway_url: HttpUrl = Field(default="http://127.0.0.1:3000", alias="FEDIFY_GATEWAY_URL")
    internal_http_host: str = Field(default="127.0.0.1", alias="INTERNAL_HTTP_HOST")
    internal_http_port: int = Field(default=8080, alias="INTERNAL_HTTP_PORT")
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
    # Comma-separated Discord guild IDs allowed to use slash commands. Empty
    # means the bot remains usable in any guild where it is installed.
    discord_guild_allowlist: list[str] = Field(default_factory=list, alias="DISCORD_GUILD_ALLOWLIST")

    @model_validator(mode="after")
    def _resolve_public_base_url(self) -> "Settings":
        """Resolve one public origin while preserving legacy upgrade inputs.

        Existing deployments may still have the two historical public URL
        variables. They are accepted only when they agree, because silently
        selecting one would change actor IDs or registration URLs.
        """
        candidates = [
            str(value).rstrip("/")
            for value in (
                self.public_base_url,
                self.legacy_public_bridge_base_url,
                self.legacy_fedify_origin,
            )
            if value is not None
        ]
        if not candidates:
            self.public_base_url = HttpUrl("http://127.0.0.1:8080")
            return self

        if len(set(candidates)) != 1:
            raise ValueError(
                "PUBLIC_BASE_URL, PUBLIC_BRIDGE_BASE_URL, and FEDIFY_ORIGIN must match"
            )
        self.public_base_url = HttpUrl(candidates[0])
        return self

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


    @field_validator("discord_guild_allowlist", mode="before")
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
                "DISCORD_GUILD_ALLOWLIST must contain comma-separated decimal Discord guild IDs"
            )
        return entries

    @property
    def normalized_public_base_url(self) -> str:
        """Return the canonical public origin without a trailing slash.

        `model_construct()` is used by a few low-level scenario fixtures and
        bypasses validators, so retain a narrow fallback to the historical
        attribute names for those compatibility-only objects.
        """
        value = (
            self.public_base_url
            or self.legacy_public_bridge_base_url
            or self.legacy_fedify_origin
            or self.__dict__.get("public_bridge_base_url")
            or self.__dict__.get("fedify_origin")
            or "http://127.0.0.1:8080"
        )
        return str(value).rstrip("/")

    @property
    def normalized_public_bridge_base_url(self) -> str:
        """Keep the former bridge-base accessor as a compatibility alias."""
        return self.normalized_public_base_url

    @property
    def normalized_fedify_origin(self) -> str:
        """Keep the former federation-origin accessor as a compatibility alias."""
        return self.normalized_public_base_url

    @property
    def public_bridge_base_url(self) -> str:
        """Expose the former field name for adapters not yet migrated."""
        return self.normalized_public_base_url

    @property
    def fedify_origin(self) -> str:
        """Expose the former field name for gateway-facing adapters."""
        return self.normalized_public_base_url

    @property
    def resolved_discord_oauth_redirect_uri(self) -> str:
        """Return the explicit OAuth callback or derive it from the public origin."""
        if self.discord_oauth_redirect_uri is not None:
            return str(self.discord_oauth_redirect_uri)
        return f"{self.normalized_public_base_url}/auth/discord/callback"
