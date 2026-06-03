"""Helpers for moderator-facing community identity labels.

The bridge persists several community identifiers: raw ActivityPub actor URLs,
optional Lemmy-local names, and cached handles. Discord command output should
prefer the compact relay form ``slug@instance`` so moderators can see both the
community slug and the owning instance without reading a full URL.
"""

from __future__ import annotations

from urllib.parse import unquote, urlparse


def community_relay_label(
    *,
    actor_id: str | None,
    name: str | None = None,
    handle: str | None = None,
) -> str:
    """Return a compact ``slug@instance`` label for command output.

    Handles are preferred when they are already stored because they preserve
    the remote instance selected during discovery. Raw actor URLs are the next
    safest source; the helper extracts the final ``/c/<slug>`` path segment and
    combines it with the URL hostname. The plain name is only a fallback because
    it does not identify the instance by itself.
    """
    normalized_handle = _normalize_handle(handle)
    if normalized_handle is not None:
        return normalized_handle

    actor_label = _label_from_actor_url(actor_id)
    if actor_label is not None:
        return actor_label

    if name:
        return name
    if actor_id:
        return actor_id
    return "unknown-community"


def _normalize_handle(handle: str | None) -> str | None:
    """Normalize stored Lemmy handles by removing the ActivityPub bang prefix."""
    if not handle:
        return None
    cleaned = handle.strip()
    if not cleaned:
        return None
    return cleaned[1:] if cleaned.startswith("!") else cleaned


def _label_from_actor_url(actor_id: str | None) -> str | None:
    """Extract ``slug@host`` from an ActivityPub community actor URL."""
    if not actor_id:
        return None
    parsed = urlparse(actor_id)
    if not parsed.hostname:
        return None

    # Lemmy community actor URLs normally end in /c/<slug>. The fallback to the
    # final path segment keeps compatible software readable without assuming a
    # vendor-specific route beyond the common actor URL shape.
    path_parts = [part for part in parsed.path.split("/") if part]
    if not path_parts:
        return None
    slug = path_parts[-1]
    if slug == "c" and len(path_parts) >= 2:
        slug = path_parts[-2]
    slug = unquote(slug)
    return f"{slug}@{parsed.hostname}"
