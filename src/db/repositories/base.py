"""Shared repository infrastructure for bridge persistence."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from sqlalchemy.orm import Session


class BaseRepository:
    """Use the Database-owned session provider for repository operations."""

    def __init__(self, session_provider: Callable[[], AbstractContextManager[Session]]) -> None:
        self._session_provider = session_provider

    def session(self) -> AbstractContextManager[Session]:
        """Return one Database-owned transactional session context."""
        return self._session_provider()
