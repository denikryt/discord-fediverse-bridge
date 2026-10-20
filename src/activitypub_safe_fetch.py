"""Public-network-only ActivityPub document retrieval for inbound parent backfill.

The URL is untrusted federation input.  This module binds DNS validation to
socket selection; URL-only or preliminary DNS checks do not prevent rebinding.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from typing import Any

import aiohttp
from aiohttp.abc import AbstractResolver
from yarl import URL

# Parent-post metadata should be small. Large remote representations are not
# needed to reconstruct a missing Discord thread and may exhaust bridge memory.
MAX_AP_DOCUMENT_BYTES = 1024 * 1024


def _require_public_ip(value: str) -> None:
    """Reject all non-globally-routable socket destinations, including mapped IPv4."""
    try:
        address = ipaddress.ip_address(value.split('%', 1)[0])
    except ValueError as exc:
        raise ValueError('remote ActivityPub socket address is not an IP') from exc
    if not address.is_global or '%' in value:
        raise ValueError('remote ActivityPub socket address is not public')


def _admit_public_ap_url(value: str) -> URL:
    """Require a credential-free HTTPS URL before any network operation."""
    try:
        url = URL(value)
        host = url.host
        if (url.scheme != 'https' or not host or url.user is not None
            or url.password is not None or url.fragment or not url.is_absolute()):
            raise ValueError('invalid remote ActivityPub HTTPS URL')
        # aiohttp bypasses custom DNS resolvers for literal IP hosts. Validate
        # those literals here and check their sockaddr again in socket_factory.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            # Reject local single-label names; FQDN DNS answers are checked at dial.
            if '.' not in host or host.endswith('.local'):
                raise ValueError('remote ActivityPub hostname is not public') from None
        else:
            _require_public_ip(host)
        return url
    except (TypeError, ValueError) as exc:
        raise ValueError('invalid or unsafe ActivityPub backfill destination') from exc


class PublicOnlyResolver(AbstractResolver):
    """Resolve for one connection attempt, rejecting mixed public/private answers.

    The aiohttp connector consumes these returned numeric addresses directly.
    DNS caching is disabled in the caller, so each new HTTP attempt revalidates
    the effective route rather than trusting an old hostname preflight.
    """

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_UNSPEC) -> list[dict[str, Any]]:
        """Return only checked A/AAAA addresses for the original TLS hostname."""
        answers = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP,
        )
        if not answers:
            raise ValueError('remote ActivityPub destination has no DNS records')
        # Reject the entire mixed RRset, not just the private record; otherwise
        # fallback address-selection behavior could reach an untrusted endpoint.
        for _, _, _, _, sockaddr in answers:
            _require_public_ip(sockaddr[0])
        return [
            {'hostname': host, 'host': sockaddr[0], 'port': sockaddr[1],
             'family': family_id, 'proto': proto, 'flags': socket.AI_NUMERICHOST}
            for family_id, _, proto, _, sockaddr in answers
        ]

    async def close(self) -> None:
        """No cached resolver state or external resource requires shutdown."""


def _public_socket(addr_info: tuple) -> socket.socket:
    """Recheck the numeric address actually selected for the outbound socket."""
    family, kind, proto, _, sockaddr = addr_info
    _require_public_ip(sockaddr[0])
    return socket.socket(family, kind, proto)


async def fetch_public_ap_object(value: str) -> dict[str, Any]:
    """Fetch one bounded AP JSON document without private routes or redirects.

    This is an unsigned public-object GET only; it does not authenticate an
    Actor or inner Create.  The caller must still verify canonical object ID,
    type and provenance before any Discord side effect.
    """
    url = _admit_public_ap_url(value)
    connector = aiohttp.TCPConnector(
        resolver=PublicOnlyResolver(), use_dns_cache=False, force_close=True,
        socket_factory=_public_socket,
    )
    # TLS remains bound to the original URL hostname; the custom resolver only
    # pins the numeric socket destination. Proxy env and 3xx redirects are not
    # trusted federation routing authority.
    async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
        async with session.get(
            url, headers={'Accept': 'application/activity+json, application/ld+json'},
            allow_redirects=False, timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if 300 <= response.status < 400:
                raise ValueError('remote ActivityPub redirect is forbidden')
            if not 200 <= response.status < 300:
                raise ValueError(f'remote ActivityPub fetch returned HTTP {response.status}')
            if response.content_length is not None and response.content_length > MAX_AP_DOCUMENT_BYTES:
                raise ValueError('remote ActivityPub document exceeds size limit')
            data = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                data.extend(chunk)
                if len(data) > MAX_AP_DOCUMENT_BYTES:
                    raise ValueError('remote ActivityPub document exceeds size limit')
            try:
                parsed = json.loads(data)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError('remote ActivityPub document is not JSON') from exc
            if not isinstance(parsed, dict):
                raise ValueError('remote ActivityPub document must be an object')
            return parsed
