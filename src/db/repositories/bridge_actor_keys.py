"""Persistence for the single bridge ActivityPub actor signing keypair."""

from __future__ import annotations

from sqlalchemy import select

from ...models import BridgeActorKey
from .base import BaseRepository

SUPPORTED_KEY_FORMATS = {"jwk", "pem"}
SUPPORTED_ALGORITHMS = {"RSASSA-PKCS1-v1_5"}


class BridgeActorKeyRepository(BaseRepository):
    """Store and retrieve the bridge actor keypair without exposing secrets."""

    def get(self) -> BridgeActorKey | None:
        """Return the only bridge actor key row, if one has been initialized."""
        with self.session() as session:
            return session.get(BridgeActorKey, 1)

    def get_by_actor_url(self, actor_url: str) -> BridgeActorKey | None:
        """Return the bridge actor key row for one canonical actor URL."""
        with self.session() as session:
            return session.scalar(
                select(BridgeActorKey).where(BridgeActorKey.actor_url == actor_url)
            )

    def create(
        self,
        *,
        actor_url: str,
        key_id: str,
        key_format: str,
        algorithm: str,
        public_key_data: str,
        private_key_data: str,
    ) -> BridgeActorKey:
        """Persist one validated bridge keypair in a self-owned transaction."""
        self._validate(
            key_format=key_format,
            algorithm=algorithm,
            public_key_data=public_key_data,
            private_key_data=private_key_data,
        )
        with self.session() as session:
            row = BridgeActorKey(
                id=1,
                actor_url=actor_url,
                key_id=key_id,
                key_format=key_format,
                algorithm=algorithm,
                public_key_data=public_key_data,
                private_key_data=private_key_data,
            )
            session.add(row)
            session.flush()
            return row

    @staticmethod
    def _validate(
        *,
        key_format: str,
        algorithm: str,
        public_key_data: str,
        private_key_data: str,
    ) -> None:
        """Reject unsupported or empty key material before it reaches storage."""
        if key_format not in SUPPORTED_KEY_FORMATS:
            raise ValueError("unsupported bridge actor key format")
        if algorithm not in SUPPORTED_ALGORITHMS:
            raise ValueError("unsupported bridge actor key algorithm")
        if not public_key_data.strip() or not private_key_data.strip():
            raise ValueError("bridge actor key material must not be empty")
