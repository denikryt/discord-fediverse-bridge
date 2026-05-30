"""Database infrastructure and repository container for bridge persistence."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from . import migrations, schema
from .repositories import (
    ActivityPubObjectRepository,
    CommunityActorBanRepository,
    BridgeActorFollowRepository,
    DiscordFanoutGroupRepository,
    DiscordDirectoryRepository,
    EventReceiptRepository,
    LegacyLemmyMappingRepository,
    LocalCommunityContentRepository,
    LocalCommunityRelayRepository,
    LocalCommunityRepository,
    LocalCommunitySurfaceRepository,
    LocalSubscriberRepository,
    MessageMappingRepository,
    RegistrationSessionRepository,
    RemoteActorRepository,
    RemoteSubscriberRepository,
    RemoteSubscriptionRepository,
    UserRepository,
)


class Database:
    """Own engine/session/schema lifecycle and expose domain repositories."""

    def __init__(self, url: str) -> None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, future=True, connect_args=connect_args)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False, class_=Session)
        self.local_communities = LocalCommunityRepository(self.session)
        self.community_actor_bans = CommunityActorBanRepository(self.session)
        self.remote_subscribers = RemoteSubscriberRepository(self.session)
        self.local_subscribers = LocalSubscriberRepository(self.session)
        self.local_community_content = LocalCommunityContentRepository(self.session)
        self.local_community_surfaces = LocalCommunitySurfaceRepository(self.session)
        self.local_community_relay = LocalCommunityRelayRepository(self.session)
        self.remote_subscriptions = RemoteSubscriptionRepository(self.session)
        self.bridge_actor_follows = BridgeActorFollowRepository(self.session)
        self.event_receipts = EventReceiptRepository(self.session)
        self.users = UserRepository(self.session)
        self.registration_sessions = RegistrationSessionRepository(self.session)
        self.message_mappings = MessageMappingRepository(self.session)
        self.activitypub_objects = ActivityPubObjectRepository(self.session)
        self.remote_actors = RemoteActorRepository(self.session)
        self.legacy_lemmy_mappings = LegacyLemmyMappingRepository(self.session)
        self.discord_fanout_groups = DiscordFanoutGroupRepository(self.session)
        self.discord_directory = DiscordDirectoryRepository(self.session)

    def create_all(self) -> None:
        """Create the full clean-schema set required by the current codebase."""
        schema.create_all(self.engine)

    def migrate(self) -> None:
        """Apply additive schema migrations that create_all cannot handle."""
        migrations.migrate(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Yield one transactional session with uniform cleanup semantics."""
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
