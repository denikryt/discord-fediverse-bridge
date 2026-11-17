"""Strict opt-in Fluxer instance configuration; no SDK or network side effects."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .db.repositories.platform_surfaces import PlatformSurfaceRepository


@dataclass(frozen=True)
class FluxerConnectorConfig:
    """One trusted transport identity and its private IPC credential, never a bot token."""

    connector_key: str
    origin: str
    rpc_url: str
    transport_secret: str = field(repr=False)
    allow_insecure_local_development: bool = False
    forum_applied_tags: tuple[tuple[str, tuple[str, ...]], ...] = ()


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
    if not isinstance(entries, list) or len(entries) > 1:
        raise ValueError("FLUXER_CONNECTORS_JSON must be an array of at most one trusted connector")

    connectors: list[FluxerConnectorConfig] = []
    fields = {
        "connector_key", "origin", "rpc_url", "transport_secret",
        "allow_insecure_local_development", "forum_applied_tags",
    }
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - fields:
            raise ValueError("Invalid Fluxer connector configuration fields")
        values = tuple(entry.get(name) for name in (
            "connector_key", "origin", "rpc_url", "transport_secret"
        ))
        if any(not isinstance(value, str) or not value or value != value.strip() for value in values):
            raise ValueError("Fluxer connector requires nonempty string key, endpoints and secrets")
        key, requested_origin, rpc_url, transport_secret = values
        # Discord's bootstrap connector key is reserved even on an empty v0
        # file: accepting it would migrate before transactional admission fails.
        if key == "discord-primary":
            raise ValueError("Fluxer connector key is reserved for Discord")
        # Node rejects shorter private bearers before SDK startup. Refuse at
        # Python settings admission instead, before opening SQLite or AP keys.
        if len(transport_secret) < 8:
            raise ValueError("Fluxer transport bearer is too short")
        # WHATWG URL preserves a final DNS dot, while SQLite canonical_origin
        # strips it. Disallow that spelling on both sides before persistence.
        if (urlsplit(requested_origin).hostname or "").endswith("."):
            raise ValueError("Fluxer origin cannot end with a DNS root dot")
        if not isinstance(entry.get("allow_insecure_local_development", False), bool):
            raise ValueError("Fluxer development HTTP opt-in must be boolean")
        allow_http = entry.get("allow_insecure_local_development", False)
        origin = PlatformSurfaceRepository.canonical_origin(requested_origin)
        origin_parts = urlsplit(origin)
        if origin_parts.scheme != "https" and not (
            allow_http and origin_parts.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("Fluxer requires HTTPS outside explicit loopback development")
        # The one Node sidecar shares the bridge network namespace. Do not
        # allow a non-loopback destination or credential-bearing RPC URL.
        try:
            rpc = urlsplit(rpc_url)
            if (
                rpc.scheme != "http"
                or not rpc.hostname
                or rpc.username is not None
                or rpc.password is not None
                or rpc.path or rpc.query or rpc.fragment
                or "?" in rpc_url or "#" in rpc_url
                or rpc.port is None or rpc.port < 1
                or rpc.hostname != "127.0.0.1"
            ):
                raise ValueError("Invalid Fluxer private RPC address")
        except ValueError as error:
            raise ValueError("Invalid Fluxer private RPC address") from error
        configured_tags = entry.get("forum_applied_tags", {})
        if not isinstance(configured_tags, dict):
            raise ValueError("Fluxer forum applied tags must be a forum-ID map")
        forum_tags: list[tuple[str, tuple[str, ...]]] = []
        for forum_id, tag_ids in configured_tags.items():
            if (not isinstance(forum_id, str)
                or re.fullmatch(r"[A-Za-z0-9_-]+", forum_id) is None
                or not isinstance(tag_ids, list) or len(tag_ids) > 5
                or any(not isinstance(tag, str)
                       or re.fullmatch(r"[A-Za-z0-9_-]+", tag) is None
                       for tag in tag_ids)
                or len(tag_ids) != len(set(tag_ids))):
                # Selection is immutable and cannot be repaired by filtering.
                raise ValueError("Invalid Fluxer forum applied tag selection")
            forum_tags.append((forum_id, tuple(tag_ids)))
        connectors.append(FluxerConnectorConfig(
            connector_key=key,
            origin=origin,
            rpc_url=rpc_url,
            transport_secret=transport_secret,
            allow_insecure_local_development=allow_http,
            forum_applied_tags=tuple(sorted(forum_tags)),
        ))
    return tuple(connectors)
