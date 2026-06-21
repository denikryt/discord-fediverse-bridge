"""Admission policy for remote ActivityPub delivery endpoints."""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlparse


class RemoteDeliveryEndpointError(ValueError):
    """Raised when a remote inbox URL is not safe for outbound delivery."""


def admit_remote_delivery_endpoint(
    endpoint: str, *, local_origin: str | None = None
) -> str:
    """Validate one remote inbox/sharedInbox URL before persistence or delivery.

    ActivityPub actor documents provide inbox URLs, but those documents are still
    remote-controlled input. The bridge must not send signed outbound requests to
    local, private, metadata, credentialed, fragmented, or same-origin URLs.
    """
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RemoteDeliveryEndpointError(
            "remote delivery endpoint must be absolute https"
        )
    if parsed.username or parsed.password:
        raise RemoteDeliveryEndpointError(
            "remote delivery endpoint must not include credentials"
        )
    if parsed.fragment:
        raise RemoteDeliveryEndpointError(
            "remote delivery endpoint must not include a fragment"
        )
    if local_origin is not None and _origin(endpoint) == _origin(local_origin):
        raise RemoteDeliveryEndpointError("remote delivery endpoint must not target local origin")
    if _blocked_hostname(parsed.hostname or ""):
        raise RemoteDeliveryEndpointError(
            "remote delivery endpoint must not target local or private networks"
        )
    return endpoint


def _origin(value: str) -> str:
    """Return scheme/netloc origin for a URL accepted by the caller."""
    parsed = urlparse(value)
    return f"{parsed.scheme}://{parsed.netloc}".lower()


def _blocked_hostname(hostname: str) -> bool:
    """Return whether a hostname is an obvious local/private target."""
    host = hostname.strip("[]").lower()
    if host in {"localhost", "metadata.google.internal"}:
        return True
    try:
        address = ip_address(host)
    except ValueError:
        # Deterministic tests avoid DNS. Hostname DNS rebinding defense belongs
        # at the network adapter; this conservative list still blocks common
        # local/metadata names before persistence.
        return False
    return (
        address.is_loopback
        or address.is_link_local
        or address.is_private
        or address.is_unspecified
        or address.is_reserved
    )
