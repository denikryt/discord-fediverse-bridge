from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from ...models import (
    ActivityPubEventReceipt,
    RegistrationSession,
    User,
)
from .base import BaseRepository


"""Registered user identity persistence."""


class UserRepository(BaseRepository):
    """Persist the users domain."""

    def create_user(
            self,
            *,
            discord_user_id: str,
            activitypub_username: str,
            actor_url: str,
            inbox_url: str,
            outbox_url: str,
            followers_url: str,
            public_key_pem: str,
            private_key_pem: str,
        ) -> User:
            """Create the shared identity record for one registered Discord user."""
            with self.session() as session:
                user = User(
                    discord_user_id=discord_user_id,
                    activitypub_username=activitypub_username,
                    actor_url=actor_url,
                    inbox_url=inbox_url,
                    outbox_url=outbox_url,
                    followers_url=followers_url,
                    public_key_pem=public_key_pem,
                    private_key_pem=private_key_pem,
                )
                session.add(user)
                session.flush()
                return user

    def get_user_by_discord_user_id(self, discord_user_id: str) -> User | None:
            """Load the registered user that owns one Discord account ID."""
            with self.session() as session:
                return session.scalar(
                    select(User).where(User.discord_user_id == discord_user_id)
                )

    def get_user_by_activitypub_username(
            self, activitypub_username: str
        ) -> User | None:
            """Load the registered user that owns one local AP username."""
            with self.session() as session:
                return session.scalar(
                    select(User).where(
                        User.activitypub_username == activitypub_username
                    )
                )

    def get_user_by_actor_url(self, actor_url: str) -> User | None:
            """Load the registered user that owns one actor URL."""
            with self.session() as session:
                return session.scalar(select(User).where(User.actor_url == actor_url))

    def list_users(self) -> list[User]:
            """Return all registered users in stable creation order."""
            # User identity export needs a deterministic ordering so repeated dumps
            # can be diffed and restored without hidden row-order changes.
            with self.session() as session:
                return list(
                    session.scalars(select(User).order_by(User.created_at, User.id))
                )
