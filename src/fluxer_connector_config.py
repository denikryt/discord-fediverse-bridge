"""Strict opt-in Fluxer instance configuration; no SDK or network side effects."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .db.repositories.platform_surfaces import PlatformSurfaceRepository


@dataclass(frozen=True)
class FluxerConnectorConfig:
    """One immutable transport identity and its process-only credentials."""

    connector_key: str
    origin: str
    rpc_url: str
    bot_token: str = field(repr=False)
    transport_secret: str = field(repr=False)
    allow_insecure_local_development: bool = False


def parse_fluxer_connectors(raw: str | None) -> tuple[FluxerConnectorConfig, ...]:
    """Parse an exact JSON array, rejecting bad deployments before SDK startup.

    A missing opt-in field produces no clients. The caller must keep the raw
    field secret and must not interpolate input values into validation errors.
    """
    if raw is None or raw == "":
        return ()
    try:
        entries = json.loads(raw)
    except (ValueError, TypeError) as error:
        raise ValueError("FLUXER_CONNECTORS_JSON must be valid JSON") from error
    if not isinstance(entries, list) or len(entries) > 8:
        raise ValueError("FLUXER_CONNECTORS_JSON must be an array of at most eight connectors")

    connectors: list[FluxerConnectorConfig] = []
    keys: set[str] = set()
    origins: set[str] = set()
    fields = {
        "connector_key", "origin", "rpc_url", "bot_token",
        "transport_secret", "allow_insecure_local_development",
    }
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - fields:
            raise ValueError("Invalid Fluxer connector configuration fields")
        values = tuple(entry.get(name) for name in (
            "connector_key", "origin", "rpc_url", "bot_token", "transport_secret"
        ))
        if any(not isinstance(value, str) or not value or value != value.strip() for value in values):
            raise ValueError("Fluxer connector requires nonempty string key, endpoints and secrets")
        key, requested_origin, rpc_url, bot_token, transport_secret = values
        if not isinstance(entry.get("allow_insecure_local_development", False), bool):
            raise ValueError("Fluxer development HTTP opt-in must be boolean")
        allow_http = entry.get("allow_insecure_local_development", False)
        origin = PlatformSurfaceRepository.canonical_origin(requested_origin)
        origin_parts = urlsplit(origin)
        if origin_parts.scheme != "https" and not (
            allow_http and origin_parts.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("Fluxer requires HTTPS outside explicit loopback development")
        # RPC must remain on an operator-controlled loopback/Compose endpoint;
        # credentials in URLs or implicit redirects would leak bridge secrets.
        try:
            rpc = urlsplit(rpc_url)
            if (
                rpc.scheme not in {"http", "https"}
                or not rpc.hostname
                or rpc.username is not None
                or rpc.password is not None
                or rpc.path or rpc.query or rpc.fragment
                or "?" in rpc_url or "#" in rpc_url
                or rpc.port is None or rpc.port < 1
                or (rpc.hostname not in {"localhost", "127.0.0.1", "::1"}
                    and "." in rpc.hostname)
            ):
                raise ValueError("Invalid Fluxer private RPC address")
        except ValueError as error:
            raise ValueError("Invalid Fluxer private RPC address") from error
        if key in keys or origin in origins:
            raise ValueError("Duplicate Fluxer connector key or canonical origin")
        keys.add(key)
        origins.add(origin)
        connectors.append(FluxerConnectorConfig(
            connector_key=key,
            origin=origin,
            rpc_url=rpc_url,
            bot_token=bot_token,
            transport_secret=transport_secret,
            allow_insecure_local_development=allow_http,
        ))
    return tuple(connectors)
