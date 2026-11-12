"""Bounded, policy-checked proof for historical NodeBB Group root membership.

A reply's ``context`` and ``audience`` cannot authorize its missing root.
Only the exact root object in the verified Group's official category outbox
is accepted here; an incomplete collection is a deferred, not a proof.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from ..nodebb_discovery import AP_ACCEPT, NodeBBDiscoveryError, SecureNodeBBJSON, _origin

MAX_OUTBOX_PAGES = 4
MAX_OUTBOX_ITEMS = 96


def _uri(value: Any) -> str | None:
    """Extract one AP identifier without guessing from a URL slug."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("id"), str):
        return value["id"]
    return None


def _outbox_object_id(item: Any, group_actor_id: str) -> str | None:
    """Unwrap an official Group Announce's object, but never trust its target alone."""
    if not isinstance(item, dict) or item.get("type") != "Announce":
        return None
    if _uri(item.get("actor")) != group_actor_id:
        return None
    target = item.get("object")
    if isinstance(target, dict) and target.get("type") == "Create":
        target = target.get("object")
    return _uri(target)


async def fetch_verified_historical_root(
    *, root_id: str, group_actor_id: str, policy: object,
    reply_context: str | None = None,
    fetch: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    """Return an old root only after official bounded category-outbox membership.

    Reject inaccessible, spoofed, unrelated and incompletely proven roots.
    With no supplied fetch adapter, every HTTPS fetch uses the discovery
    adapter's DNS-on-connect SSRF defence, response bounds and policy checks.
    """
    try:
        group_origin = _origin(group_actor_id)
        if fetch is None:
            group_fetch = SecureNodeBBJSON(group_origin, policy)
        else:
            group_fetch = fetch
        actor = await group_fetch(group_actor_id, AP_ACCEPT)
        if actor.get("id") != group_actor_id or actor.get("type") != "Group":
            return None
        outbox_id = _uri(actor.get("outbox"))
        if not outbox_id or _origin(outbox_id) != group_origin:
            return None
        # The outbox is a concrete Group-owned resource, not an arbitrary URI
        # from reply addressing. Only a small, explicitly bounded collection
        # window can establish positive membership.
        page_url: str | None = outbox_id
        seen: set[str] = set()
        items_count = 0
        found = False
        for _ in range(MAX_OUTBOX_PAGES):
            if not page_url or page_url in seen or _origin(page_url) != group_origin:
                break
            seen.add(page_url)
            page = await group_fetch(page_url, AP_ACCEPT)
            if page.get("type") not in {"OrderedCollection", "OrderedCollectionPage", "Collection", "CollectionPage"}:
                return None
            if _uri(page.get("id")) != page_url:
                return None
            entries = page.get("orderedItems", page.get("items", []))
            if not isinstance(entries, list):
                return None
            for entry in entries:
                items_count += 1
                if items_count > MAX_OUTBOX_ITEMS:
                    return None
                if _outbox_object_id(entry, group_actor_id) == root_id:
                    found = True
                    break
            if found:
                break
            nxt = _uri(page.get("next"))
            if nxt is None and page_url == outbox_id:
                nxt = _uri(page.get("first"))
            page_url = nxt
        if not found:
            return None
        root_origin = _origin(root_id)
        root_fetch = group_fetch if root_origin == group_origin else (
            fetch if fetch is not None else SecureNodeBBJSON(root_origin, policy)
        )
        root = await root_fetch(root_id, AP_ACCEPT)
        if root.get("id") != root_id or root.get("type") not in {"Note", "Article", "Page"}:
            return None
        if root.get("inReplyTo") or _uri(root.get("audience")) != group_actor_id:
            return None
        if reply_context is not None and _uri(root.get("context")) != reply_context:
            return None
        author = _uri(root.get("attributedTo"))
        if not author or urlsplit(author).scheme != "https" or not urlsplit(author).hostname:
            return None
        return root
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, NodeBBDiscoveryError):
        # Unknown/partial/cross-origin evidence never authorizes Discord work.
        return None
