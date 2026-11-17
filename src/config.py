"""Environment-backed settings for the Python bridge process."""

from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from typing import Literal

from pydantic import Field, HttpUrl, field_validator, model_validator
from pydantic_settings import BaseSettings, DotEnvSettingsSource, SettingsConfigDict
from pydantic_settings.main import ENV_FILE_SENTINEL


class _PlatformDotEnvSource(DotEnvSettingsSource):
    """Read each .env path once, retaining blanks only for BRIDGE_PLATFORM.

    Filtering each file before Pydantic merges it preserves the old
    ignore-empty behavior for all other fields across multiple .env files.
    """

    def _read_env_file(self, file_path: Path) -> Mapping[str, str | None]:
        """Keep explicit empty platform selectors without rereading the input."""
        parsed = super()._read_env_file(file_path)
        selector = self.settings_cls.model_fields['bridge_platform']
        names = {name for _, name, _ in self._extract_field_info(
            selector, 'bridge_platform',
        )}
        # The final .env file may deliberately clear the selector, but may not
        # clear legacy optional credentials when env_ignore_empty was enabled.
        return {name: value for name, value in parsed.items()
                if value != '' or name in names}


class Settings(BaseSettings):
    """Validate one active platform and the bridge's shared deployment settings."""

    # Settings centralize the bridge contract with its environment so both the
    # bot runtime and the internal HTTP API read the same values.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=False,
    )

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings,
        file_secret_settings,
    ):
        """Preserve an empty platform selector at native Pydantic priorities."""
        selector = settings_cls.model_fields['bridge_platform']
        names = {name for _, name, _ in env_settings._extract_field_info(
            selector, 'bridge_platform',
        )}
        # The process source has already normalized case and loaded env once.
        # Keep blank BRIDGE_PLATFORM; restore ignore-empty for all other fields.
        env_settings.env_vars = {
            name: value for name, value in env_settings.env_vars.items()
            if value != '' or name in names
        }
        return (init_settings, env_settings, dotenv_settings, file_secret_settings)

    @classmethod
    def _settings_init_sources(cls, *args, **kwargs):
        """Replace the dotenv loader exactly once on pinned Pydantic Settings 2.14.1.

        The hook is private and absent in the formerly declared 2.8.0 floor.
        Keep pyproject.toml/uv.lock at the verified version and retest before
        changing it; Docker installs from pyproject rather than uv.lock.
        """
        selected_file = kwargs.get('_env_file', ENV_FILE_SENTINEL)
        if selected_file == ENV_FILE_SENTINEL:
            selected_file = cls.model_config.get('env_file')
        # Pydantic creates the default dotenv source eagerly. Disable its file
        # read, then put a single-pass selective source in the same position.
        # Even an explicit _env_ignore_empty=True must not hide BRIDGE_PLATFORM.
        # Other fields still ignore blanks via the per-source filters above.
        sources, init_kwargs = super()._settings_init_sources(
            *args, **{**kwargs, '_env_file': None, '_env_ignore_empty': False},
        )
        result = list(sources)
        for index, source in enumerate(result):
            if isinstance(source, DotEnvSettingsSource):
                result[index] = _PlatformDotEnvSource(
                    cls,
                    env_file=selected_file,
                    env_file_encoding=source.env_file_encoding,
                    dotenv_filtering=source.dotenv_filtering,
                    case_sensitive=source.case_sensitive,
                    env_prefix=source.env_prefix,
                    env_prefix_target=source.env_prefix_target,
                    env_nested_delimiter=source.env_nested_delimiter,
                    env_nested_max_split=source.env_nested_max_split,
                    env_ignore_empty=False,
                    env_parse_none_str=source.env_parse_none_str,
                    env_parse_enums=source.env_parse_enums,
                )
                break
        return tuple(result), init_kwargs

    # A service owns exactly one platform; the default remains old Discord.
    bridge_platform: Literal["discord", "fluxer"] = Field(
        default="discord", alias="BRIDGE_PLATFORM"
    )
    discord_token: str | None = Field(default=None, alias="DISCORD_TOKEN")
    # Disabled unless the operator explicitly supplies validated connector JSON.
    fluxer_connectors_json: str | None = Field(
        default=None, alias="FLUXER_CONNECTORS_JSON", repr=False
    )

    database_url: str = Field(default="sqlite:///./bridge.db", alias="DATABASE_URL")
    public_base_url: HttpUrl = Field(alias="PUBLIC_BASE_URL")
    fedify_gateway_url: HttpUrl = Field(
        default="http://127.0.0.1:3000", alias="BRIDGE_GATEWAY_URL"
    )
    internal_http_host: str = Field(default="127.0.0.1", alias="BRIDGE_BIND_HOST")
    internal_http_port: int = Field(default=8080, alias="BRIDGE_BIND_PORT")
    discord_oauth_client_id: str = Field(default="", alias="DISCORD_OAUTH_CLIENT_ID")
    discord_oauth_client_secret: str = Field(
        default="", alias="DISCORD_OAUTH_CLIENT_SECRET"
    )
    discord_oauth_redirect_uri: HttpUrl | None = Field(
        default=None, alias="DISCORD_OAUTH_REDIRECT_URI"
    )
    registration_session_cookie_name: str = Field(
        default="bridge_registration_session", alias="REGISTRATION_SESSION_COOKIE_NAME"
    )
    registration_session_ttl_seconds: int = Field(
        default=3600, alias="REGISTRATION_SESSION_TTL_SECONDS"
    )
    fedify_shared_secret: str = Field(alias="FEDIFY_SHARED_SECRET")
    fedify_actor_identifier: str = Field(
        default="bridge", alias="FEDIFY_ACTOR_IDENTIFIER"
    )
    # Legacy bridge JWK values are accepted only for one-time database import.
    fedify_bridge_private_key_jwk_json: str | None = Field(
        default=None, alias="FEDIFY_BRIDGE_PRIVATE_KEY_JWK_JSON", repr=False
    )
    fedify_bridge_public_key_jwk_json: str | None = Field(
        default=None, alias="FEDIFY_BRIDGE_PUBLIC_KEY_JWK_JSON"
    )
    bridge_display_prefix: str = Field(
        default="[bridge]", alias="BRIDGE_DISPLAY_PREFIX"
    )
    dashboard_page_title: str = Field(
        default="Discord/Fediverse Bridge Instance",
        alias="DASHBOARD_PAGE_TITLE",
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    # Bootstrap policy remains immutable for the process lifetime. Dynamic
    # entries are persisted separately and merged by BridgePolicyService.
    federation_allowlist: list[str] = Field(
        default_factory=list, alias="FEDERATION_ALLOWLIST"
    )
    federation_blocklist: list[str] = Field(
        default_factory=list, alias="FEDERATION_BLOCKLIST"
    )
    discord_guild_allowlist: list[str] = Field(
        default_factory=list, alias="DISCORD_GUILD_ALLOWLIST"
    )
    discord_guild_blocklist: list[str] = Field(
        default_factory=list, alias="DISCORD_GUILD_BLOCKLIST"
    )
    bridge_super_admin_user_ids: list[str] = Field(
        default_factory=list, alias="BRIDGE_SUPER_ADMIN_USER_IDS"
    )
    profile_media_root: str = Field(
        default="/data/profile-media", alias="PROFILE_MEDIA_ROOT"
    )
    deployment_state_lock_path: str = Field(
        default="/data/deployment-state.lock", alias="DEPLOYMENT_STATE_LOCK_PATH"
    )
    profile_media_lock_path: str = Field(
        default="/data/profile-media.lock", alias="PROFILE_MEDIA_LOCK_PATH"
    )
    profile_media_max_upload_bytes: int = Field(
        default=10_485_760,
        ge=1_048_576,
        le=26_214_400,
        alias="PROFILE_MEDIA_MAX_UPLOAD_BYTES",
    )
    profile_media_max_pixels: int = Field(
        default=20_000_000,
        ge=1_000_000,
        le=40_000_000,
        alias="PROFILE_MEDIA_MAX_PIXELS",
    )
    profile_media_orphan_grace_seconds: int = Field(
        default=86_400, ge=300, le=604_800, alias="PROFILE_MEDIA_ORPHAN_GRACE_SECONDS"
    )
    profile_media_lock_timeout_seconds: int = Field(
        default=30, ge=1, le=300, alias="PROFILE_MEDIA_LOCK_TIMEOUT_SECONDS"
    )

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

    @field_validator(
        "discord_guild_allowlist",
        "discord_guild_blocklist",
        "bridge_super_admin_user_ids",
        mode="before",
    )
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
    def _validate_public_base_url_origin(self) -> "Settings":
        """Reject PUBLIC_BASE_URL values that are not an ActivityPub origin."""
        # physical surface mapping treats PUBLIC_BASE_URL as the ActivityPub origin, not as a
        # path prefix. Failing here prevents wrong durable actor ids later.
        parsed = urlsplit(str(self.public_base_url))
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("PUBLIC_BASE_URL must not contain userinfo")
        if parsed.path not in {"", "/"}:
            raise ValueError("PUBLIC_BASE_URL must not contain a non-root path")
        if parsed.query or parsed.fragment:
            raise ValueError(
                "PUBLIC_BASE_URL must not contain query or fragment components"
            )
        return self

    @model_validator(mode="after")
    def _validate_exclusive_platform(self) -> "Settings":
        """Reject incomplete or mixed platform ownership before runtime construction.

        Discord keeps its released credentials contract; standalone Fluxer has
        its own trusted connector and must never silently initialize Discord.
        """
        # Parsing the existing exact connector schema here prevents a malformed
        # secret from crossing into SQLite or the Node-owned SDK boundary.
        connectors = self.fluxer_connectors
        if self.bridge_platform == "discord":
            if not self.discord_token or not self.discord_token.strip():
                raise ValueError("DISCORD_TOKEN is required in Discord mode")
            if not self.bridge_super_admin_user_ids:
                raise ValueError(
                    "BRIDGE_SUPER_ADMIN_USER_IDS must contain at least one Discord user ID"
                )
            if self.fluxer_connectors_json:
                raise ValueError("Discord mode cannot configure Fluxer connectors")
        else:
            # Credentials supplied for an inactive platform indicate operator
            # confusion; failing closed is safer than silently ignoring them.
            if (
                self.discord_token
                or self.bridge_super_admin_user_ids
                or self.discord_oauth_client_id
                or self.discord_oauth_client_secret
                or self.discord_oauth_redirect_uri is not None
            ):
                raise ValueError("Fluxer-only mode cannot use Discord credentials")
            if len(connectors) != 1:
                raise ValueError("Fluxer-only mode requires exactly one trusted connector")
        return self

    @model_validator(mode="after")
    def _validate_profile_media_paths(self) -> "Settings":
        """Reject unsafe or overlapping durable-state lock and media paths."""
        from pathlib import Path

        media_root = Path(self.profile_media_root)
        deployment_lock = Path(self.deployment_state_lock_path)
        media_lock = Path(self.profile_media_lock_path)
        if (
            not media_root.is_absolute()
            or not deployment_lock.is_absolute()
            or not media_lock.is_absolute()
        ):
            raise ValueError("profile media and lock paths must be absolute")
        data_root = Path("/data").resolve()
        media_root = media_root.resolve(strict=False)
        deployment_lock = deployment_lock.resolve(strict=False)
        media_lock = media_lock.resolve(strict=False)
        for path in (media_root, deployment_lock, media_lock):
            if data_root not in (path, *path.parents):
                raise ValueError(
                    "profile media and lock paths must be contained beneath /data"
                )
        if deployment_lock == media_lock:
            raise ValueError(
                "deployment and media lifecycle lock paths must be distinct"
            )
        if media_root in (deployment_lock, *deployment_lock.parents) or media_root in (
            media_lock,
            *media_lock.parents,
        ):
            raise ValueError("lock paths must not be inside the profile media root")
        return self

    @model_validator(mode="after")
    def _validate_fluxer_configuration(self) -> "Settings":
        """Fail startup before opening a Gateway for malformed opt-in instances."""
        from .fluxer_connector_config import parse_fluxer_connectors

        parse_fluxer_connectors(self.fluxer_connectors_json)
        return self

    @property
    def fluxer_connectors(self):
        """Return canonical opt-in instances without exposing raw secret JSON."""
        from .fluxer_connector_config import parse_fluxer_connectors

        return parse_fluxer_connectors(self.fluxer_connectors_json)

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
