from __future__ import annotations

from sqlalchemy import select

from ...models import (
    MessageMapping,
    PublishedActivityObject,
    RemoteActor,
    utcnow,
)
from .base import BaseRepository


"""Remote ActivityPub actor cache persistence."""


class RemoteActorRepository(BaseRepository):
    """Persist the remote actors domain."""

    def upsert_remote_actor(
            self,
            *,
            actor_url: str,
            preferred_username: str | None,
            inbox_url: str,
            shared_inbox_url: str | None,
            public_key_pem: str,
        ) -> RemoteActor:
            """Insert or refresh one cached remote actor record in place."""
            # Remote actor fetches are repeatable, so this method updates the
            # mutable addressing and key fields instead of creating duplicates.
            with self.session() as session:
                actor = session.scalar(
                    select(RemoteActor).where(RemoteActor.actor_url == actor_url)
                )
                if actor is None:
                    actor = RemoteActor(
                        actor_url=actor_url,
                        preferred_username=preferred_username,
                        inbox_url=inbox_url,
                        shared_inbox_url=shared_inbox_url,
                        public_key_pem=public_key_pem,
                    )
                    session.add(actor)
                    session.flush()
                    return actor

                actor.preferred_username = preferred_username
                actor.inbox_url = inbox_url
                actor.shared_inbox_url = shared_inbox_url
                actor.public_key_pem = public_key_pem
                actor.last_fetched_at = utcnow()
                session.flush()
                return actor

    def get_remote_actor_by_actor_url(self, actor_url: str) -> RemoteActor | None:
            """Load the cached record for one remote ActivityPub actor."""
            with self.session() as session:
                return session.scalar(
                    select(RemoteActor).where(RemoteActor.actor_url == actor_url)
                )
