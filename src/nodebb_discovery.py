"""Bounded public NodeBB Group discovery and authenticated-target resolution.

Only this adapter reads NodeBB category/WebFinger/actor JSON. A category CID or
HTML slug is never an ActivityPub identity: each selected Group is read afresh,
validated, and checked against the active federation policy before use. The
network boundary pins DNS lookups to public addresses at socket connection.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
from ipaddress import ip_address
import json
import socket
import time
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from .bridge_policy import BridgePolicyService

AP_ACCEPT = 'application/activity+json, application/ld+json; profile="https://www.w3.org/ns/activitystreams"'
JSON_ACCEPT = "application/json"
MAX_CATEGORY_NODES = 96
MAX_CATEGORY_DEPTH = 5
MAX_CATEGORY_RESULTS = 25
MAX_RESPONSE_BYTES = 1_048_576
MAX_REDIRECTS = 3
MAX_PARALLEL_REQUESTS = 4
MAX_DISCOVERY_SECONDS = 8
CHOICE_TTL_SECONDS = 300


class NodeBBUnavailable(Exception):
    """The host did not advertise the NodeBB categories API (HTTP 404)."""


class NodeBBDiscoveryError(Exception):
    """A remote NodeBB target cannot safely be used for discovery or Follow."""

    def __init__(self, message: str, *, reason_code: str) -> None:
        """Keep a user-readable message and a bounded diagnostic code separate."""
        super().__init__(message)
        self.reason_code = reason_code


class NodeBBProbeInconclusive(NodeBBDiscoveryError):
    """The *initial* NodeBB endpoint failed before any NodeBB identity was proven.

    Generic discovery may try Lemmy after this error; explicit NodeBB choices
    still fail closed. A security-policy or malformed-document error is never
    downgraded to an inconclusive probe.
    """


def _inconclusive_nodebb_probe(error: NodeBBDiscoveryError) -> NodeBBProbeInconclusive | None:
    """Classify transport-only first-probe errors; never downgrade policy failure."""
    if error.reason_code in {"discovery_http_error", "discovery_timeout"}:
        return NodeBBProbeInconclusive(str(error), reason_code=error.reason_code)
    return None


@dataclass(frozen=True)
class NodeBBGroup:
    """A verified public NodeBB Group; CID remains only a discovery locator."""

    actor_id: str
    inbox: str
    name: str
    handle: str
    url: str | None = None


def _origin(value: str) -> str:
    """Normalize an external HTTPS origin without accepting URL credentials."""
    parsed = urlsplit(value)
    try:
        port_value = parsed.port
    except ValueError as error:
        raise NodeBBDiscoveryError("Invalid NodeBB port.", reason_code="denied_origin") from error
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
        or parsed.password is not None or parsed.fragment or port_value == 0
    ):
        raise NodeBBDiscoveryError("Only public HTTPS NodeBB origins are supported.", reason_code="denied_origin")
    # A hostname with a trailing dot is canonicalized for policy and actor comparisons.
    try:
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise NodeBBDiscoveryError("Invalid NodeBB hostname.", reason_code="denied_origin") from error
    if host.startswith("[") or host.endswith("]"):
        raise NodeBBDiscoveryError("Invalid NodeBB origin.", reason_code="denied_origin")
    host_ip = None
    try:
        host_ip = ip_address(host)
    except ValueError:
        pass
    if host_ip is not None and not host_ip.is_global:
        raise NodeBBDiscoveryError("NodeBB origin has a non-public IP address.", reason_code="denied_origin")
    if host_ip is not None and host_ip.version == 6:
        host = f"[{host}]"
    port = f":{port_value}" if port_value and port_value != 443 else ""
    return f"https://{host}{port}"


def _ensure_target(url: str, expected_origin: str, policy: BridgePolicyService) -> None:
    """Enforce exact same-origin HTTPS and federation policy before any fetch."""
    parsed = urlsplit(url)
    if (
        _origin(url) != expected_origin or parsed.username is not None or parsed.password is not None
        or not policy.federation_decision(url).allowed
    ):
        raise NodeBBDiscoveryError("NodeBB target is blocked by federation policy or origin.", reason_code="denied_origin")
    if len(url) > 2048:
        raise NodeBBDiscoveryError("NodeBB URL is too long.", reason_code="malformed_response")


class _PublicDNSResolver(AbstractResolver):
    """Reject non-public DNS answers when aiohttp resolves the connection host."""

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[dict[str, Any]]:
        """Return only public IP targets, failing closed on mixed DNS responses."""
        try:
            addresses = await asyncio.get_running_loop().getaddrinfo(
                host, port, family=family, type=socket.SOCK_STREAM
            )
        except (OSError, ValueError) as error:
            raise NodeBBDiscoveryError("NodeBB DNS lookup failed.", reason_code="discovery_http_error") from error
        if not addresses:
            raise NodeBBDiscoveryError("NodeBB DNS returned no addresses.", reason_code="discovery_http_error")
        result: list[dict[str, Any]] = []
        for family_, _, proto, _, address in addresses:
            ip = ip_address(address[0])
            # Reject even one private address instead of silently choosing a
            # different public result from a rebinding/mixed-answer response.
            if not ip.is_global or (ip.version == 6 and ip.ipv4_mapped is not None and not ip.ipv4_mapped.is_global):
                raise NodeBBDiscoveryError("NodeBB DNS resolved to a non-public address.", reason_code="denied_origin")
            result.append({
                "hostname": host, "host": str(ip), "port": port,
                "family": family_, "proto": proto, "flags": socket.AI_NUMERICHOST,
            })
        return result

    async def close(self) -> None:
        """This stateless resolver holds no open sockets."""
        return None


class SecureNodeBBJSON:
    """Fetch bounded JSON with public-IP DNS validation and no cross-origin redirects."""

    def __init__(self, origin: str, policy: BridgePolicyService) -> None:
        """Bind every subsequent request to the one verified origin and policy."""
        self.origin = _origin(origin)
        self.policy = policy
        self._semaphore = asyncio.Semaphore(MAX_PARALLEL_REQUESTS)

    async def __call__(self, url: str, accept: str) -> dict[str, Any]:
        """Fetch one bounded JSON document without reusing unvalidated DNS/IPs."""
        timeout = aiohttp.ClientTimeout(total=12, connect=5, sock_read=5)
        # The resolver is used by the actual connector: a disconnected DNS
        # preflight followed by an ordinary client would remain rebindable.
        connector = aiohttp.TCPConnector(
            resolver=_PublicDNSResolver(), use_dns_cache=False,
            force_close=True, limit=MAX_PARALLEL_REQUESTS,
        )
        async with self._semaphore, aiohttp.ClientSession(
            connector=connector, timeout=timeout, trust_env=False,
        ) as session:
            target = url
            for _ in range(MAX_REDIRECTS + 1):
                _ensure_target(target, self.origin, self.policy)
                try:
                    async with session.get(
                        target, headers={"Accept": accept}, allow_redirects=False
                    ) as response:
                        if response.status in {301, 302, 303, 307, 308}:
                            # Do not follow cross-origin URLs, even when the
                            # HTTP client would otherwise allow the redirect.
                            from urllib.parse import urljoin
                            location = response.headers.get("Location")
                            if not location:
                                raise NodeBBDiscoveryError("Invalid NodeBB redirect.", reason_code="malformed_response")
                            target = urljoin(target, location)
                            continue
                        if response.status == 404:
                            raise NodeBBUnavailable()
                        if response.status >= 400 or response.status != 200:
                            raise NodeBBDiscoveryError("NodeBB HTTP discovery failed.", reason_code="discovery_http_error")
                        if response.content_type not in {
                            "application/json", "application/activity+json", "application/ld+json"
                        }:
                            raise NodeBBDiscoveryError("Unexpected NodeBB content type.", reason_code="malformed_response")
                        if response.content_length is not None and response.content_length > MAX_RESPONSE_BYTES:
                            raise NodeBBDiscoveryError("NodeBB response is too large.", reason_code="malformed_response")
                        chunks = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            chunks.extend(chunk)
                            if len(chunks) > MAX_RESPONSE_BYTES:
                                raise NodeBBDiscoveryError("NodeBB response is too large.", reason_code="malformed_response")
                        try:
                            document = json.loads(chunks)
                        except (UnicodeDecodeError, ValueError) as error:
                            raise NodeBBDiscoveryError("NodeBB returned invalid JSON.", reason_code="malformed_response") from error
                        if not isinstance(document, dict):
                            raise NodeBBDiscoveryError("NodeBB returned an invalid document.", reason_code="malformed_response")
                        return document
                except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                    reason = "discovery_timeout" if isinstance(error, asyncio.TimeoutError) else "discovery_http_error"
                    raise NodeBBDiscoveryError("NodeBB discovery is unavailable.", reason_code=reason) from error
            raise NodeBBDiscoveryError("NodeBB redirect limit exceeded.", reason_code="malformed_response")


def _category_cid(item: dict[str, Any]) -> int | None:
    """Only genuine positive decimal CIDs are usable candidate actor routes."""
    cid = item.get("cid")
    if isinstance(cid, bool) or not isinstance(cid, (int, str)):
        return None
    text = str(cid)
    if not text.isascii() or not text.isdecimal() or len(text) > 12 or int(text) <= 0:
        return None
    return int(text)


def _category_is_candidate(item: dict[str, Any]) -> bool:
    """Reject actual visibility/federation denials, not layout-only sections."""
    # NodeBB's ``isSection`` groups a category visually on the homepage;
    # it neither revokes guest access nor prevents its children from federating.
    # Keep individual read and ActivityPub Group verification in _verified_group.
    return not any(item.get(field) in (True, 1, "1") for field in ("disabled", "private", "hidden")) and not any(
        item.get(field) in (False, 0, "0") for field in ("federationEnabled", "isFederated", "public")
    )


def _flatten_categories(items: list[Any]) -> list[dict[str, Any]]:
    """Walk a bounded tree with unique CIDs, including nested children."""
    result: list[dict[str, Any]] = []
    seen: set[int] = set()
    stack = [(item, 0) for item in reversed(items)]
    while stack and len(result) < MAX_CATEGORY_NODES:
        item, depth = stack.pop()
        if not isinstance(item, dict):
            continue
        cid = _category_cid(item)
        if cid is None or cid in seen:
            continue
        seen.add(cid)
        if _category_is_candidate(item):
            result.append(item)
        # Descend through public display sections. Do not traverse a denied
        # parent: its descendants are not independently discoverable here.
        if _category_is_candidate(item) and depth < MAX_CATEGORY_DEPTH:
            children = item.get("children", [])
            if isinstance(children, list):
                stack.extend((child, depth + 1) for child in reversed(children[:MAX_CATEGORY_NODES]))
    return result


def _actor_cid(url: str, origin: str) -> int | None:
    """Parse a canonical numeric category route, never a slug as a CID."""
    if _origin(url) != origin:
        return None
    parsed = urlsplit(url)
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2 or len(parts) > 3 or parts[0] != "category" or parsed.query or parsed.fragment:
        return None
    cid = _category_cid({"cid": parts[1]})
    if cid is not None:
        return cid
    # NodeBB's public URL often uses a `42-discussion` slug. The numeric
    # prefix is only a candidate for a fresh canonical Group fetch, never an
    # asserted actor identity inferred from the slug itself.
    if len(parts) == 2 and "-" in parts[1]:
        number, slug = parts[1].split("-", 1)
        if slug and _category_cid({"cid": number}) is not None:
            return int(number)
    return None


async def _verified_group(
    cid: int, origin: str, policy: BridgePolicyService,
    fetch: Callable[[str, str], Awaitable[dict[str, Any]]],
    *, expected_actor_id: str | None = None, name_hint: str | None = None,
    handle_hint: str | None = None,
) -> NodeBBGroup:
    """Verify anonymously readable category and canonical Group/Inbox evidence."""
    # api/categories proves only the guest 'find' privilege. The read API
    # separately checks the guest 'read' privilege, so require both evidence.
    category_url = f"{origin}/api/category/{cid}"
    actor_url = f"{origin}/category/{cid}"
    _ensure_target(category_url, origin, policy)
    _ensure_target(actor_url, origin, policy)
    category = await fetch(category_url, JSON_ACCEPT)
    if not isinstance(category, dict) or _category_cid(category) != cid or not _category_is_candidate(category):
        raise NodeBBDiscoveryError("NodeBB category is not publicly readable.", reason_code="no_public_groups")
    actor = await fetch(actor_url, AP_ACCEPT)
    if not isinstance(actor, dict) or actor.get("type") != "Group" or actor.get("id") != actor_url:
        raise NodeBBDiscoveryError("NodeBB did not return a canonical Group.", reason_code="malformed_response")
    if expected_actor_id is not None and actor_url != expected_actor_id:
        raise NodeBBDiscoveryError("NodeBB actor identity changed.", reason_code="malformed_response")
    inbox = actor.get("inbox")
    if not isinstance(inbox, str):
        raise NodeBBDiscoveryError("NodeBB Group has no inbox.", reason_code="malformed_response")
    _ensure_target(inbox, origin, policy)
    name = actor.get("name") or category.get("name") or name_hint
    preferred = actor.get("preferredUsername") or category.get("handle") or handle_hint
    if not isinstance(name, str) or not name or not isinstance(preferred, str) or not preferred:
        raise NodeBBDiscoveryError("NodeBB Group lacks public identity metadata.", reason_code="malformed_response")
    host = urlsplit(origin).netloc
    if not host:
        raise NodeBBDiscoveryError("NodeBB origin is invalid.", reason_code="denied_origin")
    public_url = actor.get("url")
    if not isinstance(public_url, str) or _origin(public_url) != origin:
        public_url = None
    return NodeBBGroup(
        actor_id=actor_url, inbox=inbox, name=name,
        handle=f"!{preferred.lstrip('!')}@{host}", url=public_url,
    )


async def fetch_nodebb_groups(
    origin: str, *, policy_service: BridgePolicyService,
    fetch_json: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None,
    query: str = "",
) -> list[NodeBBGroup]:
    """Discover at most 25 publicly readable and verified NodeBB category Groups."""
    normalized = _origin(origin)
    _ensure_target(f"{normalized}/api/categories", normalized, policy_service)
    fetch = fetch_json or SecureNodeBBJSON(normalized, policy_service)
    deadline = time.monotonic() + MAX_DISCOVERY_SECONDS
    try:
        payload = await asyncio.wait_for(
            fetch(f"{normalized}/api/categories", JSON_ACCEPT), timeout=MAX_DISCOVERY_SECONDS
        )
    except asyncio.TimeoutError as error:
        # The very first request did not establish that this host is NodeBB.
        raise NodeBBProbeInconclusive("NodeBB category discovery timed out.", reason_code="discovery_timeout") from error
    except NodeBBDiscoveryError as error:
        inconclusive = _inconclusive_nodebb_probe(error)
        if inconclusive is None:
            raise  # A policy or malformed-response failure must fail closed.
        raise inconclusive from error
    raw_categories = payload.get("categories") if isinstance(payload, dict) else None
    if not isinstance(raw_categories, list):
        raise NodeBBUnavailable()  # Not a recognizable NodeBB categories API.
    candidates = _flatten_categories(raw_categories)
    needle = query.strip().lstrip("!").casefold()
    if needle:
        # Filter by public listing first: verifying the first 25 unrelated
        # categories must not hide a later matching NodeBB category.
        host = urlsplit(normalized).hostname or ""
        candidates = [item for item in candidates if any(
            needle in value.casefold() for value in (
                str(item.get("name") or ""), str(item.get("handle") or ""),
                f"{item.get('handle') or ''}@{host}",
                str(item.get("slug") or ""),
                f"{normalized}/category/{_category_cid(item)}",
                f"{normalized}/category/{item.get('slug') or ''}",
            )
        )]
    verified: list[NodeBBGroup] = []
    # Bound sequential requests (stronger than a concurrency cap) and stop as
    # soon as Discord's 25-choice budget is filled.
    for item in candidates[:48]:
        cid = _category_cid(item)
        if cid is None:
            continue
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            group = await asyncio.wait_for(
                _verified_group(
                    cid, normalized, policy_service, fetch,
                    name_hint=str(item.get("name") or ""),
                    handle_hint=str(item.get("handle") or ""),
                ), timeout=remaining,
            )
        except asyncio.TimeoutError as error:
            raise NodeBBDiscoveryError("NodeBB category discovery timed out.", reason_code="discovery_timeout") from error
        except (NodeBBUnavailable, NodeBBDiscoveryError):
            continue
        verified.append(group)
        if len(verified) >= MAX_CATEGORY_RESULTS:
            break
    return verified


async def probe_nodebb_origin(
    origin: str, *, policy_service: BridgePolicyService,
    fetch_json: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None,
) -> bool:
    """Identify a NodeBB API without traversing categories or trusting URL patterns."""
    normalized = _origin(origin)
    url = f"{normalized}/api/categories"
    _ensure_target(url, normalized, policy_service)
    fetch = fetch_json or SecureNodeBBJSON(normalized, policy_service)
    try:
        payload = await fetch(url, JSON_ACCEPT)
    except asyncio.TimeoutError as error:
        raise NodeBBProbeInconclusive("NodeBB origin probe timed out.", reason_code="discovery_timeout") from error
    except NodeBBDiscoveryError as error:
        inconclusive = _inconclusive_nodebb_probe(error)
        if inconclusive is None:
            raise  # A policy or malformed-response failure must fail closed.
        raise inconclusive from error
    if not isinstance(payload, dict) or not isinstance(payload.get("categories"), list):
        raise NodeBBUnavailable()
    return True


async def resolve_nodebb_group(
    value: str, *, origin: str, policy_service: BridgePolicyService,
    fetch_json: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None,
) -> NodeBBGroup:
    """Resolve handle, category URL or canonical actor by verified Group data."""
    normalized = _origin(origin)
    _ensure_target(f"{normalized}/api/categories", normalized, policy_service)
    fetch = fetch_json or SecureNodeBBJSON(normalized, policy_service)
    candidate = value.strip()
    parsed = urlsplit(candidate)
    expected_actor_id: str | None = None
    cid: int | None = None
    requested_handle: str | None = None
    if parsed.scheme:
        _ensure_target(candidate, normalized, policy_service)
        cid = _actor_cid(candidate, normalized)
        if cid is None:
            raise NodeBBDiscoveryError("NodeBB URL is not a category Group.", reason_code="malformed_response")
        if candidate == f"{normalized}/category/{cid}":
            expected_actor_id = candidate
    else:
        name = candidate.lstrip("!")
        if "@" in name:
            name, host = name.rsplit("@", 1)
            if not host or host.lower() not in (urlsplit(normalized).hostname, urlsplit(normalized).netloc):
                raise NodeBBDiscoveryError("NodeBB handle belongs to another origin.", reason_code="denied_origin")
        if not name or any(char in name for char in ("/", "?", "#", "@", " ")):
            raise NodeBBDiscoveryError("Invalid NodeBB category handle.", reason_code="malformed_response")
        requested_handle = name
        webfinger_url = f"{normalized}/.well-known/webfinger?resource={quote('acct:' + name + '@' + urlsplit(normalized).netloc, safe='')}"
        _ensure_target(webfinger_url, normalized, policy_service)
        payload = await fetch(webfinger_url, JSON_ACCEPT)
        if not isinstance(payload, dict) or payload.get("subject", "").casefold() != f"acct:{name}@{urlsplit(normalized).netloc}".casefold():
            raise NodeBBDiscoveryError("NodeBB WebFinger identity mismatch.", reason_code="malformed_response")
        links = payload.get("links", [])
        hrefs = [item.get("href") for item in links if isinstance(item, dict) and item.get("rel") == "self" and item.get("type") in ("application/activity+json", "application/ld+json")]
        if len(hrefs) != 1 or not isinstance(hrefs[0], str):
            raise NodeBBDiscoveryError("NodeBB WebFinger has no unique Group actor.", reason_code="malformed_response")
        _ensure_target(hrefs[0], normalized, policy_service)
        cid = _actor_cid(hrefs[0], normalized)
        if cid is None:
            raise NodeBBDiscoveryError("WebFinger did not resolve a NodeBB Group.", reason_code="malformed_response")
        expected_actor_id = hrefs[0]
    group = await _verified_group(cid, normalized, policy_service, fetch, expected_actor_id=expected_actor_id)
    if parsed.scheme and candidate != group.actor_id:
        # A visual URL must actually be advertised by the same verified Group;
        # a made-up slug cannot silently select another category.
        if candidate.rstrip("/") != (group.url or "").rstrip("/"):
            raise NodeBBDiscoveryError("NodeBB category link does not match its Group.", reason_code="malformed_response")
    if requested_handle is not None and group.handle.split("@", 1)[0].lstrip("!").casefold() != requested_handle.casefold():
        raise NodeBBDiscoveryError("NodeBB handle does not match the verified Group.", reason_code="malformed_response")
    return group


class _NodeBBChoiceCache:
    """Bounded UI-only lookup; cached values never authorize a Follow."""

    def __init__(self) -> None:
        """Store a short-lived map of canonical URL and origin by opaque key."""
        self.entries: OrderedDict[str, tuple[str, str, float]] = OrderedDict()

    def make(self, group: NodeBBGroup) -> str:
        """Encode long identities in a stable short key without truncating IDs."""
        origin = _origin(group.actor_id)
        key = hashlib.sha256(f"{origin}\0{group.actor_id}".encode()).hexdigest()[:28]
        self.entries[key] = (origin, group.actor_id, time.monotonic() + CHOICE_TTL_SECONDS)
        self.entries.move_to_end(key)
        while len(self.entries) > 128:
            self.entries.popitem(last=False)
        return f"nodebb-key:{key}"

    def get(self, encoded: str) -> tuple[str, str] | None:
        """Return a fresh cached locator, never verification or policy proof."""
        entry = self.entries.get(encoded)
        if entry is None:
            return None
        origin, actor, expires = entry
        if expires <= time.monotonic():
            self.entries.pop(encoded, None)
            return None
        return origin, actor


_CHOICE_CACHE = _NodeBBChoiceCache()


def nodebb_autocomplete_choices(groups: list[NodeBBGroup], query: str) -> list[tuple[str, str]]:
    """Return Discord-compliant labels and exact canonical actor choices."""
    choices: list[tuple[str, str]] = []
    needle = query.strip().lstrip("!").casefold()
    for group in groups:
        candidates = (group.name, group.handle.lstrip("!"), group.actor_id, group.url or "")
        if needle and not any(needle in candidate.casefold() for candidate in candidates):
            continue
        suffix = f" ({group.handle.lstrip('!')})"
        budget = max(0, 100 - len(suffix))
        label = (group.name[:budget] + suffix)[:100] if budget else suffix[:100]
        value = f"nodebb:{group.actor_id}"
        if len(value) > 100:
            value = _CHOICE_CACHE.make(group)
        choices.append((label, value))
        if len(choices) == MAX_CATEGORY_RESULTS:
            break
    return choices


def decode_nodebb_choice(value: str) -> tuple[str, str]:
    """Decode only an actor locator; resolution must redo policy and Group fetch."""
    if value.startswith("nodebb-key:"):
        resolved = _CHOICE_CACHE.get(value.split(":", 1)[1])
        if resolved is None:
            raise NodeBBDiscoveryError(
                "This NodeBB selection expired. Search again or paste the category URL.",
                reason_code="malformed_response",
            )
        return resolved
    if value.startswith("nodebb:"):
        actor = value.removeprefix("nodebb:")
        return _origin(actor), actor
    raise NodeBBDiscoveryError("Invalid NodeBB selection.", reason_code="malformed_response")
