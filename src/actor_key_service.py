"""Actor-key access and bridge-identity bootstrap services."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from .config import Settings
from .db import Database
from .models import BridgeActorKey, LocalCommunity, User
from .registration_service import generate_rsa_keypair_pem

LOGGER = logging.getLogger(__name__)
ALGORITHM = "RSASSA-PKCS1-v1_5"


@dataclass(frozen=True, slots=True)
class ActorKeyMaterial:
    """Expose one actor keypair with its explicit persisted representation."""

    key_format: str
    algorithm: str
    public_key_data: str
    private_key_data: str


class ActorKeyService:
    """Resolve actor keys while preserving owner-specific database storage."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def get_bridge_actor_keys(self) -> ActorKeyMaterial:
        """Return the initialized bridge actor keypair or fail explicitly."""
        row = self.database.bridge_actor_keys.get()
        if row is None:
            raise LookupError("bridge actor keypair is not initialized")
        return _bridge_material(row)

    def get_user_actor_keys(self, user_id: int) -> ActorKeyMaterial:
        """Return the PEM keypair owned by one registered-user row."""
        user = self.database.users.get_user_by_id(user_id)
        if user is None:
            raise LookupError("registered user does not exist")
        return _user_material(user)

    def get_local_community_actor_keys(self, local_community_id: int) -> ActorKeyMaterial:
        """Return the PEM keypair owned by one local-community row."""
        community = self.database.local_communities.get_local_community_by_id(local_community_id)
        if community is None:
            raise LookupError("local community does not exist")
        return _community_material(community)


class BridgeActorKeyBootstrap:
    """Ensure one stable bridge actor keypair exists before runtime readiness."""

    def __init__(self, *, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings

    def ensure(self) -> BridgeActorKey:
        """Load, import, or generate the bridge key without replacing identity."""
        existing = self.database.bridge_actor_keys.get()
        if existing is not None:
            LOGGER.info(
                "Bridge actor key loaded actor_url=%s format=%s",
                existing.actor_url,
                existing.key_format,
            )
            return existing

        private_jwk = self.settings.fedify_bridge_private_key_jwk_json
        public_jwk = self.settings.fedify_bridge_public_key_jwk_json
        if (private_jwk is None) != (public_jwk is None):
            raise RuntimeError(
                "Both legacy FEDIFY bridge JWK values are required for one-time import"
            )

        actor_url = f"{self.settings.normalized_fedify_origin}/actors/{self.settings.fedify_actor_identifier}"
        key_id = f"{actor_url}#main-key"
        if private_jwk is not None and public_jwk is not None:
            _validate_jwk_json(private_jwk, "private")
            _validate_jwk_json(public_jwk, "public")
            row = self.database.bridge_actor_keys.create(
                actor_url=actor_url,
                key_id=key_id,
                key_format="jwk",
                algorithm=ALGORITHM,
                public_key_data=public_jwk,
                private_key_data=private_jwk,
            )
            LOGGER.info("Bridge actor key imported actor_url=%s format=jwk", actor_url)
            return row

        public_pem, private_pem = generate_rsa_keypair_pem()
        row = self.database.bridge_actor_keys.create(
            actor_url=actor_url,
            key_id=key_id,
            key_format="pem",
            algorithm=ALGORITHM,
            public_key_data=public_pem,
            private_key_data=private_pem,
        )
        LOGGER.info("Bridge actor key generated actor_url=%s format=pem", actor_url)
        return row


def _validate_jwk_json(value: str, label: str) -> None:
    """Validate one legacy JWK as a JSON object without logging its contents."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Legacy bridge {label} JWK is invalid JSON") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise RuntimeError(f"Legacy bridge {label} JWK must be a non-empty JSON object")


def _bridge_material(row: BridgeActorKey) -> ActorKeyMaterial:
    """Map one bridge-key row to the shared application-level shape."""
    return ActorKeyMaterial(row.key_format, row.algorithm, row.public_key_data, row.private_key_data)


def _user_material(user: User) -> ActorKeyMaterial:
    """Map one user row to the shared application-level shape."""
    return ActorKeyMaterial("pem", ALGORITHM, user.public_key_pem, user.private_key_pem)


def _community_material(community: LocalCommunity) -> ActorKeyMaterial:
    """Map one local-community row to the shared application-level shape."""
    return ActorKeyMaterial("pem", ALGORITHM, community.public_key_pem, community.private_key_pem)
