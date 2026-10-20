"""Connector-scoped channel identity and transactional typed-claim persistence."""

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select, update, String
from sqlalchemy.orm import Session

from ...platform_types import ConnectorRef
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

        Stage 01 migration owns creation of the connector row. Runtime must
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
        """Normalize identity metadata without discovering or contacting an API."""
        parsed = urlsplit(origin)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Connector origin requires HTTP(S) scheme/host and no path, credentials, query or fragment"
            )
        hostname = parsed.hostname.lower()
        host = f"[{hostname}]" if ":" in hostname else hostname
        if parsed.port is not None:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme.lower(), host, "", "", ""))

    @staticmethod
    def add_connector(
        session: Session,
        *,
        connector_key: str,
        platform: str,
        instance_origin: str | None = None,
    ) -> PlatformConnector:
        """Provision one immutable connector; uniqueness is finally enforced by SQLite."""
        if not isinstance(connector_key, str) or not connector_key.strip():
            raise ValueError("Connector key must be a nonempty opaque string")
        if platform == "discord" and instance_origin is not None:
            raise ValueError("Discord connector origin must be NULL")
        if platform == "fluxer":
            if instance_origin is None:
                raise ValueError("Fluxer connector requires an instance origin")
            instance_origin = PlatformSurfaceRepository.canonical_origin(
                instance_origin
            )
        if platform not in {"discord", "fluxer"}:
            raise ValueError("Unsupported connector platform")
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
