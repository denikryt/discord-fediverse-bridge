"""Registration policy and actor-identity creation for Discord users."""

from __future__ import annotations

import re
import secrets
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .db import Database

USERNAME_PATTERN = re.compile(r"^[a-z0-9_-]+$")
RESERVED_USERNAMES = {"bridge", "admin", "system"}


class RegistrationError(Exception):
    """Signal a registration request that violates Stage 5 policy."""


@dataclass(slots=True)
class RegisteredActor:
    """Describe the local actor identity created or reused for one Discord user."""

    discord_user_id: str
    activitypub_username: str
    actor_url: str
    inbox_url: str
    outbox_url: str
    followers_url: str


class RegistrationService:
    """Own username policy, actor URL building, and user-row creation."""

    # RegistrationService keeps the route handlers thin and testable by moving
    # validation and actor-record creation into one explicit policy boundary.
    def __init__(
        self,
        *,
        database: Database,
        base_url: str,
        keypair_generator: Callable[[], tuple[str, str]] | None = None,
    ) -> None:
        self.database = database
        self.base_url = base_url.rstrip("/")
        self.keypair_generator = keypair_generator or generate_rsa_keypair_pem

    def create_or_get_user(
        self, *, discord_user_id: str, requested_username: str
    ) -> tuple[str, RegisteredActor]:
        """Create a local actor or return the existing actor for one Discord user."""
        # Duplicate Discord IDs must resolve to the existing actor so a repeated
        # web registration cannot create split ownership for one account.
        existing_by_discord_id = self.database.users.get_user_by_discord_user_id(
            discord_user_id
        )
        if existing_by_discord_id is not None:
            return "existing", self._as_registered_actor(existing_by_discord_id)

        username = requested_username.strip().lower()
        self.validate_username(username)

        if self.database.users.get_user_by_activitypub_username(username) is not None:
            raise RegistrationError("That username is already taken.")

        actor_url, inbox_url, outbox_url, followers_url = self.build_actor_urls(
            username
        )
        public_key_pem, private_key_pem = self.keypair_generator()
        created = self.database.users.create_user(
            discord_user_id=discord_user_id,
            activitypub_username=username,
            actor_url=actor_url,
            inbox_url=inbox_url,
            outbox_url=outbox_url,
            followers_url=followers_url,
            public_key_pem=public_key_pem,
            private_key_pem=private_key_pem,
        )
        return "created", self._as_registered_actor(created)

    def validate_username(self, username: str) -> None:
        """Validate one requested local ActivityPub username."""
        if not username:
            raise RegistrationError("Username is required.")
        if username in RESERVED_USERNAMES:
            raise RegistrationError("That username is reserved.")
        if not USERNAME_PATTERN.fullmatch(username):
            raise RegistrationError(
                "Username must use only lowercase letters, numbers, underscores, or hyphens."
            )

    def build_actor_urls(self, username: str) -> tuple[str, str, str, str]:
        """Build the canonical local actor URLs for one registered username."""
        # Registered users use the Fedify actor namespace as their canonical
        # ActivityPub identity. Object URLs can still live under /users, but
        # actor/key ownership must stay aligned with /actors/{username}.
        actor_url = f"{self.base_url}/actors/{username}"
        return (
            actor_url,
            f"{actor_url}/inbox",
            f"{actor_url}/outbox",
            f"{actor_url}/followers",
        )

    def actor_handle(self, username: str) -> str:
        """Build the local `@user@domain` handle shown in success pages."""
        hostname = self.base_url.removeprefix("https://").removeprefix("http://")
        return f"@{username}@{hostname}"

    def _as_registered_actor(self, user: object) -> RegisteredActor:
        """Convert one DB user row into the public registration result shape."""
        return RegisteredActor(
            discord_user_id=getattr(user, "discord_user_id"),
            activitypub_username=getattr(user, "activitypub_username"),
            actor_url=getattr(user, "actor_url"),
            inbox_url=getattr(user, "inbox_url"),
            outbox_url=getattr(user, "outbox_url"),
            followers_url=getattr(user, "followers_url"),
        )


def generate_session_token() -> str:
    """Return one opaque token suitable for a browser registration cookie."""
    return secrets.token_urlsafe(32)


def generate_oauth_state() -> str:
    """Return one opaque OAuth state token for a Discord login round-trip."""
    return secrets.token_urlsafe(24)


def generate_rsa_keypair_pem() -> tuple[str, str]:
    """Generate one RSA keypair in PEM form using the local OpenSSL binary."""
    # Fedify expects real PEM key material from the shared DB, so Stage 5 uses
    # OpenSSL here instead of storing placeholders that later publish flows
    # could not sign with.
    with tempfile.TemporaryDirectory(prefix="bridge-register-keys-") as temp_dir:
        private_key_path = Path(temp_dir) / "private.pem"
        public_key_path = Path(temp_dir) / "public.pem"
        subprocess.run(
            [
                "openssl",
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
                "-out",
                str(private_key_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "openssl",
                "pkey",
                "-in",
                str(private_key_path),
                "-pubout",
                "-out",
                str(public_key_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return public_key_path.read_text(), private_key_path.read_text()
