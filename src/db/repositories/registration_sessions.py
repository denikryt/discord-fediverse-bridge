from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from ...models import (
    ActivityPubEventReceipt,
    RegistrationSession,
    User,
)
from .base import BaseRepository


"""Browser and OAuth registration session persistence."""


class RegistrationSessionRepository(BaseRepository):
    """Persist the registration sessions domain."""

    def create_registration_session(
            self, *, session_token: str, expires_at: datetime
        ) -> RegistrationSession:
            """Create one server-side registration session row."""
            with self.session() as session:
                registration_session = RegistrationSession(
                    session_token=session_token,
                    expires_at=expires_at,
                )
                session.add(registration_session)
                session.flush()
                return registration_session

    def get_registration_session_by_token(
            self, session_token: str | None
        ) -> RegistrationSession | None:
            """Load one registration session by its opaque browser token."""
            if session_token is None:
                return None
            with self.session() as session:
                return session.scalar(
                    select(RegistrationSession).where(
                        RegistrationSession.session_token == session_token
                    )
                )

    def update_registration_session_oauth_state(
            self, *, session_token: str, oauth_state: str, expires_at: datetime
        ) -> RegistrationSession:
            """Persist the latest OAuth state for one browser registration flow."""
            # OAuth state must be stored server-side so the callback can reject
            # forged or replayed redirect attempts from outside Discord.
            with self.session() as session:
                registration_session = session.scalar(
                    select(RegistrationSession).where(
                        RegistrationSession.session_token == session_token
                    )
                )
                if registration_session is None:
                    raise RuntimeError(
                        f"Missing registration session for token {session_token}"
                    )
                registration_session.oauth_state = oauth_state
                registration_session.expires_at = expires_at
                registration_session.status = "oauth_started"
                session.flush()
                return registration_session

    def update_registration_session_discord_identity(
            self,
            *,
            session_token: str,
            discord_user_id: str,
            discord_username: str,
            discord_avatar_url: str | None,
            expires_at: datetime,
        ) -> RegistrationSession:
            """Attach the authenticated Discord identity to one session."""
            with self.session() as session:
                registration_session = session.scalar(
                    select(RegistrationSession).where(
                        RegistrationSession.session_token == session_token
                    )
                )
                if registration_session is None:
                    raise RuntimeError(
                        f"Missing registration session for token {session_token}"
                    )
                registration_session.discord_user_id = discord_user_id
                registration_session.discord_username = discord_username
                registration_session.discord_avatar_url = discord_avatar_url
                registration_session.expires_at = expires_at
                registration_session.status = "discord_authenticated"
                session.flush()
                return registration_session

    def mark_registration_session_completed(
            self, *, session_token: str, activitypub_username: str, expires_at: datetime
        ) -> RegistrationSession:
            """Mark one registration session complete after the user row is created."""
            with self.session() as session:
                registration_session = session.scalar(
                    select(RegistrationSession).where(
                        RegistrationSession.session_token == session_token
                    )
                )
                if registration_session is None:
                    raise RuntimeError(
                        f"Missing registration session for token {session_token}"
                    )
                registration_session.activitypub_username = activitypub_username
                registration_session.expires_at = expires_at
                registration_session.status = "completed"
                session.flush()
                return registration_session
