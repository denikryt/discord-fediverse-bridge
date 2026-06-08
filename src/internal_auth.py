"""Shared bearer-token authentication for private bridge HTTP routes."""

from __future__ import annotations

import secrets

from fastapi import HTTPException, status


def validate_internal_bearer(*, authorization: str | None, shared_secret: str) -> None:
    """Validate one internal Bearer credential without timing-sensitive equality."""
    prefix = "Bearer "
    if authorization is None or not authorization.startswith(prefix):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid internal authorization",
        )
    provided = authorization[len(prefix) :]
    if not secrets.compare_digest(provided, shared_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid internal authorization",
        )
