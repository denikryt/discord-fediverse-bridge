"""Connector-scoped channel identity and transactional typed-claim persistence."""

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import ipaddress
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...content_sync.surface_resolution import ResolvedForumSource
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select, update, String, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from ...platform_types import ConnectorRef, ChannelRef
from ...models import (
    ChannelSurface,
    PlatformConnector,
    LocalCommunity,
    LocalSubscriber,
    ChannelCommunitySubscription,
    PersonalFeedBinding,
)
from .base import BaseRepository

OWNER_MODELS = {
    "local_community_id": LocalCommunity,
    "local_subscriber_id": LocalSubscriber,
    "remote_subscription_id": ChannelCommunitySubscription,
    "personal_feed_binding_id": PersonalFeedBinding,
}


def preflight_instance_mode(
    database_url: str, *, platform: str,
    connector_key: str | None = None, instance_origin: str | None = None,
) -> None:
    """Read pre-existing SQLite ownership without migrating or adopting AP keys.

    Call only while the runtime and deployment leases exclude competing writers.
    The existing migration runner still owns schema validation and transactional
    connector admission; this boundary prevents an unsafe *prior* commit.
    """
    if platform not in {"discord", "fluxer"}:
        raise ValueError("Invalid exclusive platform mode")
    database_name = make_url(database_url).database
    if not database_name:
        raise ValueError("Mode preflight requires a file-backed SQLite database")
    path = Path(database_name).resolve(strict=False)
    if not path.exists():
        return
    if not path.is_file():
        raise RuntimeError("Unsupported existing database path for platform preflight")
    if path.stat().st_size == 0:
        return

    # SQLite URI mode=ro prevents an existing Discord v0 database being
    # modified before Fluxer identity ownership is established. No connection
    # constructed here may open a writer or create a missing database.
    uri = path.as_uri() + "?mode=ro"
    engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))
    try:
        from ..migrations import (
            _validate_current_v1_schema, validate_released_v0,
        )
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN")
            version = connection.exec_driver_sql("PRAGMA user_version").scalar_one()
            names = set(connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'"
            ).scalars())
            other_objects = connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='view'"
            ).first()
            if not names and version == 0 and other_objects is None:
                return
            if version == 0:
                # A released v0 database belongs to Discord even when all of
                # its tables happen to be empty. Fluxer must never upgrade it.
                validate_released_v0(connection)
                if platform == "fluxer":
                    raise RuntimeError("Fluxer mode refuses a released Discord v0 database")
                return
            if version != 1:
                raise RuntimeError("Unknown database schema/version for platform mode")
            _validate_current_v1_schema(connection)
            rows = connection.exec_driver_sql(
                "SELECT connector_key,platform,instance_origin FROM platform_connectors"
            ).all()
            fluxer = [row for row in rows if row[1] == "fluxer"]
            if platform == "discord":
                if fluxer:
                    raise RuntimeError("Discord mode refuses a Fluxer-bound database")
                return
            if any(row[0] != "discord-primary" and row[1] != "fluxer" for row in rows):
                raise RuntimeError("Fluxer mode refuses unknown connector ownership")
            if len(fluxer) > 1:
                raise RuntimeError("Fluxer mode refuses multiple persisted bindings")
            if fluxer and (connector_key is None or instance_origin is None or
                           fluxer[0][0] != connector_key or fluxer[0][2] != instance_origin):
                raise RuntimeError("Fluxer connector identity differs from persisted binding")
            has_key = connection.exec_driver_sql(
                "SELECT 1 FROM bridge_actor_keys LIMIT 1"
            ).first() is not None
            if has_key and not fluxer:
                # Even a Discord instance with no messages has publicly owned
                # the bridge's actor URL and signing key since first startup.
                raise RuntimeError("Fluxer mode refuses an unbound service actor key")
            if not fluxer:
                # A first Fluxer admission may inherit only the inert v1
                # discord-primary bootstrap metadata row, no domain history.
                for table in sorted(names - {"platform_connectors", "bridge_actor_keys"}):
                    if connection.exec_driver_sql(f'SELECT 1 FROM "{table}" LIMIT 1').first():
                        raise RuntimeError("Fluxer mode refuses pre-existing domain state")
            # An already-bound Fluxer DB may own its own physical copies, but
            # never current Discord-specific author, registration or channel data.
            if fluxer:
                discord_tables = (
                    "users", "registration_sessions", "personal_feed_bindings",
                    "discord_guild_snapshots", "discord_channel_snapshots",
                    "inbound_discord_deliveries", "local_communities",
                    "local_subscribers", "channel_community_subscriptions",
                )
                for table in discord_tables:
                    if connection.exec_driver_sql(f'SELECT 1 FROM "{table}" LIMIT 1').first():
                        raise RuntimeError("Fluxer mode refuses Discord-owned domain rows")
                if connection.exec_driver_sql(
                    "SELECT 1 FROM platform_channel_surfaces AS s "
                    "JOIN platform_connectors AS c ON c.id=s.connector_id "
                    "WHERE c.connector_key='discord-primary' LIMIT 1"
                ).first():
                    raise RuntimeError("Fluxer mode refuses Discord-owned channel surfaces")
    except sqlite3.DatabaseError as error:
        raise RuntimeError("Invalid database file for platform preflight") from error
    finally:
        engine.dispose()


@dataclass(frozen=True)
class ChannelPlacement:
    """Read a durable physical location and its database-computed ownership role."""

    id: int
    connector_id: int
    connector_key: str
    platform: str
    external_channel_id: str
    external_guild_id: str | None
    kind: str
    role: str | None
    local_community_id: int | None = None
    local_subscriber_id: int | None = None
    remote_subscription_id: int | None = None
    personal_feed_binding_id: int | None = None


def discord_claim_query(model, owner_column):
    """Select domain rows only through matching current primary Discord claims."""
    query = (
        select(model)
        .join(ChannelSurface, getattr(ChannelSurface, owner_column) == model.id)
        .join(PlatformConnector, PlatformConnector.id == ChannelSurface.connector_id)
        .where(
            PlatformConnector.connector_key == "discord-primary",
            PlatformConnector.platform == "discord",
            (
                ChannelSurface.kind.in_(("text", "announcement", "unknown"))
                if owner_column == "personal_feed_binding_id"
                else ChannelSurface.kind == "forum"
            ),
        )
    )
    if owner_column != "local_community_id":
        # Projection alone never admits a recipient. Once the typed claim is
        # proven, reject malformed projections before any Discord SDK call.
        query = query.where(
            model.discord_channel_id > 0,
            model.discord_channel_id.cast(String) == ChannelSurface.external_channel_id,
        )
    if owner_column == "personal_feed_binding_id":
        query = query.where(
            model.discord_guild_id > 0,
            model.discord_guild_id.cast(String) == ChannelSurface.external_guild_id,
        )
    return query


def discord_copy_query(model, *, source=False):
    """Scope historical copy lookups without requiring a current channel owner."""
    column = model.source_connector_id if source else model.connector_id
    return (
        select(model)
        .join(PlatformConnector, PlatformConnector.id == column)
        .where(
            PlatformConnector.connector_key == "discord-primary",
            PlatformConnector.platform == "discord",
        )
    )


class PlatformSurfaceRepository(BaseRepository):
    """Resolve immutable locations and mutate claims only in caller-owned transactions."""


    def get_discord_primary_connector(self) -> ConnectorRef:
        """Read the persisted Discord transport identity or fail before Gateway startup.

        connector-scoped persistence migration owns creation of the connector row. Runtime must
        never construct an adapter from an assumed row that was not verified.
        """
        with self.session() as session:
            row = session.scalar(select(PlatformConnector).where(
                PlatformConnector.connector_key == 'discord-primary'
            ))
            if row is None or row.platform != 'discord' or row.instance_origin is not None:
                raise RuntimeError('discord-primary connector metadata is missing or incompatible')
            return ConnectorRef(row.connector_key, row.platform, row.instance_origin)

    @staticmethod
    def canonical_origin(origin: str) -> str:
        """Canonicalize an instance identity without DNS/network side effects.

        The returned bytes are persisted and compared by SQLite; noncanonical
        direct SQL values are outside the repository's normalization contract.
        """
        if (
            not isinstance(origin, str)
            or not origin
            or any(char.isspace() for char in origin)
            or "?" in origin
            or "#" in origin
        ):
            raise ValueError("Invalid connector origin URL")
        try:
            parsed = urlsplit(origin)
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not parsed.netloc
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Connector origin requires scheme and host only")
            port = parsed.port
            hostname = parsed.hostname
            if port is not None and port == 0:
                raise ValueError("Invalid connector origin port")
            if ":" in hostname:
                # Forbid address-zone scoping and normalize all IPv6 spellings.
                if "%" in hostname:
                    raise ValueError("IPv6 zone identifiers are not instance identities")
                normalized_host = f"[{ipaddress.IPv6Address(hostname).compressed}]"
            else:
                host = hostname.removesuffix(".")
                if not host or host.endswith("."):
                    raise ValueError("Invalid DNS hostname")
                try:
                    normalized_host = str(ipaddress.IPv4Address(host))
                except ipaddress.AddressValueError:
                    # IDNA is applied by label before saving, never by a DNS
                    # lookup; labels must remain valid ASCII host labels.
                    normalized_host = host.encode("idna").decode("ascii").lower()
                    if len(normalized_host) > 253 or any(
                        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                        for label in normalized_host.split(".")
                    ):
                        raise ValueError("Invalid DNS hostname")
            scheme = parsed.scheme.lower()
            if port is not None and not (
                (scheme == "http" and port == 80)
                or (scheme == "https" and port == 443)
            ):
                normalized_host += f":{port}"
            return urlunsplit((scheme, normalized_host, "", "", ""))
        except (UnicodeError, ValueError) as error:
            raise ValueError("Invalid connector origin URL") from error

    @staticmethod
    def admit_single_trusted_fluxer(
        session: Session, *, connector_key: str | None, instance_origin: str | None,
    ) -> PlatformConnector | None:
        """Admit at most one saved Fluxer binding before starting external clients.

        The caller owns the transaction. An existing incompatible identity is
        preserved for operator migration, not silently deleted or rebound.
        """
        rows = session.scalars(
            select(PlatformConnector)
            .where(PlatformConnector.platform == "fluxer")
            .limit(2)
        ).all()
        if len(rows) > 1:
            raise ValueError("Fluxer deployment has multiple persisted connector identities")
        if connector_key is None and instance_origin is None:
            # An explicitly disabled connector leaves its historical binding
            # intact and never adds or starts a replacement.
            return None
        if connector_key is None or instance_origin is None:
            raise ValueError("Fluxer configured identity is incomplete")
        instance_origin = PlatformSurfaceRepository.canonical_origin(instance_origin)
        if rows and (
            rows[0].connector_key != connector_key
            or rows[0].instance_origin != instance_origin
        ):
            raise ValueError("Fluxer configured instance conflicts with persisted identity")
        return PlatformSurfaceRepository.add_connector(
            session, connector_key=connector_key, platform="fluxer",
            instance_origin=instance_origin,
        )

    @staticmethod
    def add_connector(
        session: Session,
        *,
        connector_key: str,
        platform: str,
        instance_origin: str | None = None,
    ) -> PlatformConnector:
        """Register an immutable connector, reusing only an identical binding.

        Callers own the transaction. The permanent SQLite unique index is the
        final arbiter of a concurrent different-key/same-origin registration.
        """
        if not isinstance(connector_key, str) or not connector_key.strip():
            raise ValueError("Connector key must be a nonempty opaque string")
        if platform == "discord" and instance_origin is not None:
            raise ValueError("Discord connector origin must be NULL")
        if platform == "fluxer":
            if instance_origin is None:
                raise ValueError("Fluxer connector requires an instance origin")
            instance_origin = PlatformSurfaceRepository.canonical_origin(instance_origin)
        if platform not in {"discord", "fluxer"}:
            raise ValueError("Unsupported connector platform")
        existing = session.scalar(
            select(PlatformConnector).where(PlatformConnector.connector_key == connector_key)
        )
        if existing is not None:
            if existing.platform != platform or existing.instance_origin != instance_origin:
                raise ValueError("Connector key is bound to a different platform or origin")
            return existing
        row = PlatformConnector(
            connector_key=connector_key,
            platform=platform,
            instance_origin=instance_origin,
        )
        session.add(row)
        session.flush()
        return row

    @staticmethod
    def ensure_channel(
        session: Session,
        *,
        connector_key: str,
        external_channel_id: str,
        external_guild_id: str | None = None,
        kind: str = "unknown",
    ) -> ChannelSurface:
        """Register verified physical metadata without overwriting incompatible evidence."""
        if (
            not isinstance(external_channel_id, str)
            or not external_channel_id
            or kind not in {"forum", "text", "announcement", "unknown"}
        ):
            raise ValueError("Invalid channel identity or kind")
        connector = session.scalar(
            select(PlatformConnector).where(
                PlatformConnector.connector_key == connector_key
            )
        )
        if connector is None:
            raise ValueError("Unknown connector")
        row = session.scalar(
            select(ChannelSurface).where(
                ChannelSurface.connector_id == connector.id,
                ChannelSurface.external_channel_id == external_channel_id,
            )
        )
        if row is None:
            row = ChannelSurface(
                connector_id=connector.id,
                external_channel_id=external_channel_id,
                external_guild_id=external_guild_id,
                kind=kind,
            )
            session.add(row)
        else:
            if (
                row.external_guild_id is not None
                and external_guild_id is not None
                and row.external_guild_id != external_guild_id
            ):
                raise ValueError("Channel guild identity conflict")
            if row.kind != "unknown" and kind != "unknown" and row.kind != kind:
                raise ValueError("Channel kind conflict")
            if row.external_guild_id is None:
                row.external_guild_id = external_guild_id
            if row.kind == "unknown":
                row.kind = kind
        session.flush()
        return row

    @staticmethod
    def discord_channel(
        session: Session,
        channel_id: int,
        guild_id: int | None = None,
        kind: str = "unknown",
    ) -> ChannelSurface:
        """Resolve a genuine Discord integer only within the primary connector."""
        if (
            type(channel_id) is not int
            or channel_id <= 0
            or (guild_id is not None and (type(guild_id) is not int or guild_id <= 0))
        ):
            raise ValueError("Discord location must use positive integer IDs")
        return PlatformSurfaceRepository.ensure_channel(
            session,
            connector_key="discord-primary",
            external_channel_id=str(channel_id),
            external_guild_id=str(guild_id) if guild_id is not None else None,
            kind=kind,
        )

    @staticmethod
    def discord_copy_location(session, channel_id, guild_id=None):
        """Resolve persisted copy placement on the same connection as the copy write."""
        surface = PlatformSurfaceRepository.discord_channel(
            session, channel_id, guild_id
        )
        return {"connector_id": surface.connector_id, "channel_surface_id": surface.id}

    @staticmethod
    def discord_source_location(session, channel_id, guild_id=None):
        """Preserve a Discord source connector even if its historic channel is unknown."""
        if channel_id is None:
            connector = session.scalar(
                select(PlatformConnector).where(
                    PlatformConnector.connector_key == "discord-primary",
                    PlatformConnector.platform == "discord",
                )
            )
            if connector is None:
                raise ValueError("Missing primary Discord connector")
            return {
                "source_connector_id": connector.id,
                "source_channel_surface_id": None,
            }
        location = PlatformSurfaceRepository.discord_copy_location(
            session, channel_id, guild_id
        )
        return {
            "source_connector_id": location["connector_id"],
            "source_channel_surface_id": location["channel_surface_id"],
        }

    @staticmethod
    def discord_thread_channel(session, thread_id):
        """Resolve a real persisted thread parent; reject missing/ambiguous legacy context."""
        from ...models import (
            LocalCommunityThreadSurface,
            CommunityThreadGroupDelivery,
            PostLink,
            PersonalFeedThreadMapping,
        )

        locations = set()
        for model in (
            LocalCommunityThreadSurface,
            CommunityThreadGroupDelivery,
            PostLink,
            PersonalFeedThreadMapping,
        ):
            rows = session.scalars(
                discord_copy_query(model).where(
                    model.external_thread_id == str(thread_id)
                )
            )
            for row in rows:
                locations.add(row.channel_surface_id)
        if len(locations) != 1:
            raise ValueError("Missing or ambiguous Discord thread parent channel")
        surface = session.get(ChannelSurface, locations.pop())
        return int(surface.external_channel_id)

    @staticmethod
    def _placement(session, surface):
        """Project location metadata without mutating or caching computed role."""
        connector = session.get(PlatformConnector, surface.connector_id)
        return ChannelPlacement(
            surface.id,
            connector.id,
            connector.connector_key,
            connector.platform,
            surface.external_channel_id,
            surface.external_guild_id,
            surface.kind,
            surface.role,
            surface.local_community_id,
            surface.local_subscriber_id,
            surface.remote_subscription_id,
            surface.personal_feed_binding_id,
        )

    @staticmethod
    def resolve_forum_source(session: Session, channel: "ChannelRef") -> "ResolvedForumSource | None":
        """Delegate verified forum-source admission to the SDK-neutral boundary.

        Callers must supply a transaction and must revalidate its frozen owner
        against the current claim before any canonical or network side effect.
        """
        from ...content_sync.surface_resolution import resolve_forum_source

        return resolve_forum_source(session, channel)

    def resolve_channel(
        self, connector_key: str, external_id: str
    ) -> ChannelPlacement | None:
        """Lookup a channel by connection identity, never a globally ambiguous ID."""
        with self.session() as session:
            row = session.scalar(
                select(ChannelSurface)
                .join(PlatformConnector)
                .where(
                    PlatformConnector.connector_key == connector_key,
                    ChannelSurface.external_channel_id == external_id,
                )
            )
            return self._placement(session, row) if row is not None else None

    def get_host_placement(self, community_id: int) -> ChannelPlacement | None:
        """Return the current host independently of canonical community actor lookup."""
        with self.session() as session:
            row = session.scalar(
                select(ChannelSurface).where(
                    ChannelSurface.local_community_id == community_id
                )
            )
            return self._placement(session, row) if row is not None else None

    def get_discord_host_placement(self, community_id: int) -> ChannelPlacement | None:
        """Project a host only when its current claim belongs to primary Discord."""
        placement = self.get_host_placement(community_id)
        return (
            placement
            if placement is not None
            and placement.connector_key == "discord-primary"
            and placement.platform == "discord"
            and placement.kind == "forum"
            else None
        )

    def resolve_claim(
        self, connector_key: str, external_id: str
    ) -> ChannelPlacement | None:
        """Read a current claim; an unbound historical location has no claim."""
        placement = self.resolve_channel(connector_key, external_id)
        return (
            placement if placement is not None and placement.role is not None else None
        )

    @staticmethod
    def _claim(
        session: Session, surface_id: int, owner_column: str, owner_id: int
    ) -> ChannelSurface:
        """Validate projection and CAS the only mutable ownership state under writer lock."""
        surface = session.get(ChannelSurface, surface_id)
        owner = session.get(OWNER_MODELS[owner_column], owner_id)
        if surface is None or owner is None:
            raise ValueError("Missing channel surface or owner")
        # Admission must match recipient capabilities before reserving the
        # channel. Unknown metadata never proves forum support; historical
        # Personal Feed targets retain their existing non-forum kinds.
        allowed_kinds = (
            {"text", "announcement", "unknown"}
            if owner_column == "personal_feed_binding_id"
            else {"forum"}
        )
        if surface.kind not in allowed_kinds:
            raise ValueError("Channel kind is incompatible with typed owner")
        connector = session.get(PlatformConnector, surface.connector_id)
        if connector.platform == "discord":
            try:
                numeric_channel = int(surface.external_channel_id)
            except ValueError as exc:
                raise ValueError("Invalid Discord channel identity") from exc
            if (
                numeric_channel <= 0
                or str(numeric_channel) != surface.external_channel_id
            ):
                raise ValueError(
                    "Discord channel identity must use its canonical positive ID"
                )
        if owner_column != "local_community_id":
            expected_channel = (
                int(surface.external_channel_id)
                if connector.connector_key == "discord-primary"
                and connector.platform == "discord"
                else None
            )
            expected_guild = (
                int(surface.external_guild_id)
                if expected_channel is not None
                and surface.external_guild_id is not None
                else None
            )
            if (
                owner.discord_channel_id != expected_channel
                or owner.discord_guild_id != expected_guild
            ):
                raise ValueError(
                    "Owner Discord projection does not match connector location"
                )
        other_columns = [
            getattr(ChannelSurface, column).is_(None)
            for column in OWNER_MODELS
            if column != owner_column
        ]
        # Idempotent same-owner claims are allowed. Any other claim must remain
        # occupied regardless of owner lifecycle status (including failed).
        current = getattr(surface, owner_column)
        if current not in {None, owner_id} or any(
            getattr(surface, c) is not None for c in OWNER_MODELS if c != owner_column
        ):
            raise ValueError("Channel is already owned")
        result = session.execute(
            update(ChannelSurface)
            .where(
                ChannelSurface.id == surface_id,
                *other_columns,
                (
                    getattr(ChannelSurface, owner_column).is_(None)
                    if current is None
                    else getattr(ChannelSurface, owner_column) == owner_id
                ),
            )
            .values({owner_column: owner_id})
        )
        if result.rowcount != 1:
            raise ValueError("Channel claim changed")
        session.flush()
        session.refresh(surface)
        return surface

    @staticmethod
    def claim_local_host(session, surface_id, community_id):
        """Assign one host; DB ownership UNIQUE prevents a second host for this actor."""
        return PlatformSurfaceRepository._claim(
            session, surface_id, "local_community_id", community_id
        )

    @staticmethod
    def claim_local_subscriber(session, surface_id, subscriber_id):
        """Assign local membership while retaining the channel's durable identity."""
        return PlatformSurfaceRepository._claim(
            session, surface_id, "local_subscriber_id", subscriber_id
        )

    @staticmethod
    def claim_remote_subscription(session, surface_id, subscription_id):
        """Assign a remote owner without deriving eligibility from Follow status."""
        return PlatformSurfaceRepository._claim(
            session, surface_id, "remote_subscription_id", subscription_id
        )

    @staticmethod
    def claim_personal_feed(session, surface_id, binding_id):
        """Assign current feed placement after any previous claim is released."""
        return PlatformSurfaceRepository._claim(
            session, surface_id, "personal_feed_binding_id", binding_id
        )

    @staticmethod
    def release_claim(session, surface_id, *, owner_column, expected_owner_id):
        """Clear only the matching typed owner; retain all historical location/copies."""
        if owner_column not in OWNER_MODELS:
            raise ValueError("Unknown typed owner")
        result = session.execute(
            update(ChannelSurface)
            .where(
                ChannelSurface.id == surface_id,
                getattr(ChannelSurface, owner_column) == expected_owner_id,
            )
            .values({owner_column: None})
        )
        if result.rowcount != 1:
            raise ValueError("Channel claim changed before release")
        session.flush()
