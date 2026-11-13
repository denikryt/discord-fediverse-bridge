"""NodeBB discovery scenarios at the anonymous HTTP/Group authorization boundary.

A fake only replaces external JSON network IO. The production resolver, API
visibility checks, actor validation, policy, choice encoding and replay logic
remain in the path exercised by each observable moderator action.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from src.nodebb_discovery import (
    AP_ACCEPT,
    NodeBBDiscoveryError,
    NodeBBUnavailable,
    _CHOICE_CACHE,
    _PublicDNSResolver,
    _flatten_categories,
    decode_nodebb_choice,
    fetch_nodebb_groups,
    nodebb_autocomplete_choices,
    resolve_nodebb_group,
)

ORIGIN = "https://nodebb.example"
FIXTURES = Path(__file__).parent / "fixtures" / "nodebb"


class PublicPolicy:
    """Simulate the actual federation policy decision for remote reads."""

    def __init__(self, allowed: bool = True) -> None:
        """Keep policy changes observable in later fetch attempts."""
        self.allowed = allowed

    def federation_decision(self, origin: str):
        """Allow or block the tested origin without performing external IO."""
        return SimpleNamespace(allowed=self.allowed)


class NodeBBFixtureServer:
    """Record anonymous JSON requests with controllable visibility and identities."""

    def __init__(self) -> None:
        """Construct the public category list and verified actor from pinned fixtures."""
        self.calls: list[tuple[str, str]] = []
        self.actor = json.loads((FIXTURES / "category_actor.json").read_text())
        self.actor["id"] = f"{ORIGIN}/category/42"
        self.actor["inbox"] = f"{ORIGIN}/category/42/inbox"
        self.actor["url"] = f"{ORIGIN}/category/42/discussion"
        self.actor["preferredUsername"] = "discussion"
        self.listing = {"categories": [
            {"cid": 1, "name": "Private", "handle": "private", "private": True,
             "children": [{"cid": 11, "name": "Child of private", "handle": "hidden"}]},
            {"cid": 42, "name": "Discussion", "handle": "discussion", "children": [
                {"cid": 44, "name": "Nonfederated", "handle": "off", "isFederated": False},
                {"cid": 45, "name": "Not readable", "handle": "read-only"},
                {"cid": 46, "name": "Wrong actor type", "handle": "false"},
                {"cid": 47, "name": "Nested", "handle": "nested"},
            ]},
            {"cid": 42, "name": "Duplicate", "handle": "duplicate"},
        ]}
        self.unreadable: set[int] = {45}

    async def get(self, url: str, accept: str) -> dict:
        """Serve exact per-category read and actor JSON, or an HTTP 404."""
        self.calls.append((url, accept))
        if url == f"{ORIGIN}/api/categories":
            return self.listing
        if url.startswith(f"{ORIGIN}/.well-known/webfinger"):
            resource = parse_qs(urlsplit(url).query).get("resource", [""])[0]
            if resource != "acct:discussion@nodebb.example":
                raise NodeBBUnavailable()
            return {"subject": resource, "links": [
                {"rel": "self", "type": "application/activity+json", "href": f"{ORIGIN}/category/42"}
            ]}
        if url.startswith(f"{ORIGIN}/api/category/"):
            cid = int(url.rsplit("/", 1)[1])
            if cid in self.unreadable:
                raise NodeBBUnavailable()
            return {"cid": cid, "name": "Discussion" if cid == 42 else "Nested", "handle": "discussion" if cid == 42 else "nested"}
        if url.startswith(f"{ORIGIN}/category/"):
            cid = int(url.rsplit("/", 1)[1])
            if cid == 46:
                return {**self.actor, "id": url, "type": "Person"}
            return {
                **self.actor, "id": url, "inbox": self.actor["inbox"] if cid == 42 else f"{url}/inbox",
                "preferredUsername": "discussion" if cid == 42 else "nested",
                "name": "Discussion" if cid == 42 else "Nested",
                "url": self.actor["url"] if cid == 42 else f"{url}/nested",
            }
        raise NodeBBUnavailable()


@pytest.mark.asyncio
async def test_list_only_anonymously_readable_federative_groups_in_nested_tree() -> None:
    """Autocomplete excludes find-only categories, disabled actors and descendants of private parents."""
    server = NodeBBFixtureServer()
    groups = await fetch_nodebb_groups(ORIGIN, policy_service=PublicPolicy(), fetch_json=server.get)
    assert [item.actor_id for item in groups] == [f"{ORIGIN}/category/42", f"{ORIGIN}/category/47"]
    assert groups[0].handle == "!discussion@nodebb.example"
    assert all(accept == AP_ACCEPT for url, accept in server.calls if "/category/42" == urlsplit(url).path)
    assert all("/category/1" not in url and "/category/44" not in url for url, _ in server.calls)
    assert len(groups) <= 25
    choices = nodebb_autocomplete_choices(groups, "DISCUSSION")
    assert len(choices) == 1
    assert choices[0][1] == f"nodebb:{ORIGIN}/category/42"
    assert all(len(name) <= 100 and len(value) <= 100 for name, value in choices)
    assert nodebb_autocomplete_choices(groups, "https://nodebb.example/category/47")[0][1].endswith("/47")


@pytest.mark.asyncio
async def test_search_finds_later_category_without_verifying_first_25() -> None:
    """A matching late category is discovered within the bounded API tree."""
    server = NodeBBFixtureServer()
    server.listing = {"categories": [
        {"cid": number, "name": "Unrelated", "handle": "other"}
        for number in range(1, 70)
    ]}
    server.listing["categories"][-1] = {"cid": 69, "name": "Target", "handle": "target"}

    async def fetched(url: str, accept: str) -> dict:
        """Only the late matching Group is served with matching verified identity."""
        payload = await server.get(url, accept)
        if url == f"{ORIGIN}/category/69":
            return {**payload, "name": "Target", "preferredUsername": "target", "url": f"{url}/target"}
        if url == f"{ORIGIN}/api/category/69":
            return {**payload, "name": "Target", "handle": "target"}
        return payload

    groups = await fetch_nodebb_groups(
        ORIGIN, policy_service=PublicPolicy(), fetch_json=fetched, query="target",
    )
    assert [group.actor_id for group in groups] == [f"{ORIGIN}/category/69"]
    assert len(nodebb_autocomplete_choices(groups, "TARGET")) == 1
    assert all("/category/1" not in url for url, _ in server.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", [
    "!discussion@nodebb.example", "discussion@nodebb.example", "discussion",
    "https://nodebb.example/category/42", "https://nodebb.example/category/42/discussion",
])
async def test_manual_handle_and_actor_routes_converge_to_verified_group(reference: str) -> None:
    """Every supported manual form resolves to the same canonical Group, never its URL slug."""
    server = NodeBBFixtureServer()
    result = await resolve_nodebb_group(reference, origin=ORIGIN, policy_service=PublicPolicy(), fetch_json=server.get)
    assert result.actor_id == f"{ORIGIN}/category/42"
    assert result.inbox == f"{ORIGIN}/category/42/inbox"
    assert result.handle == "!discussion@nodebb.example"


@pytest.mark.asyncio
async def test_public_nodebb_cid_slug_requires_advertised_group_alias() -> None:
    """A NodeBB `42-discussion` page URL is a candidate, not an inferred AP identity."""
    server = NodeBBFixtureServer()
    server.actor["url"] = f"{ORIGIN}/category/42-discussion"
    group = await resolve_nodebb_group(
        f"{ORIGIN}/category/42-discussion", origin=ORIGIN,
        policy_service=PublicPolicy(), fetch_json=server.get,
    )
    assert group.actor_id == f"{ORIGIN}/category/42"
    with pytest.raises(NodeBBDiscoveryError):
        await resolve_nodebb_group(
            f"{ORIGIN}/category/42-other", origin=ORIGIN,
            policy_service=PublicPolicy(), fetch_json=server.get,
        )


@pytest.mark.asyncio
async def test_manual_private_category_and_non_group_fail_before_subscription() -> None:
    """An existing but unreadable category or Person actor must never yield a target."""
    server = NodeBBFixtureServer()
    for reference in (f"{ORIGIN}/category/45", f"{ORIGIN}/category/46"):
        with pytest.raises((NodeBBDiscoveryError, NodeBBUnavailable)):
            await resolve_nodebb_group(reference, origin=ORIGIN, policy_service=PublicPolicy(), fetch_json=server.get)
    assert all("/inbox" not in url for url, _ in server.calls)


@pytest.mark.asyncio
async def test_forged_actor_id_inbox_and_webfinger_links_fail_closed() -> None:
    """An actor on another origin, forged canonical ID or redirected inbox cannot be accepted."""
    server = NodeBBFixtureServer()
    server.actor["inbox"] = "https://127.0.0.1/inbox"
    with pytest.raises(NodeBBDiscoveryError):
        await resolve_nodebb_group(f"{ORIGIN}/category/42", origin=ORIGIN, policy_service=PublicPolicy(), fetch_json=server.get)
    server.actor["inbox"] = f"{ORIGIN}/category/42/inbox"
    server.actor["id"] = "https://evil.example/category/42"
    # Actor URL is rechecked against its canonical target even after a valid API response.
    async def forged(url: str, accept: str) -> dict:
        """Tamper only with the verified candidate Group ID at the HTTP edge."""
        payload = await server.get(url, accept)
        return {**payload, "id": server.actor["id"]} if url.endswith("/category/42") and "/api/" not in url else payload
    with pytest.raises(NodeBBDiscoveryError):
        await resolve_nodebb_group(f"{ORIGIN}/category/42", origin=ORIGIN, policy_service=PublicPolicy(), fetch_json=forged)


@pytest.mark.asyncio
async def test_policy_denial_performs_zero_fetches_and_choice_rechecks_policy() -> None:
    """Policy is applied before first HTTP and again when an old autocomplete choice is submitted."""
    server = NodeBBFixtureServer()
    policy = PublicPolicy(False)
    with pytest.raises(NodeBBDiscoveryError):
        await fetch_nodebb_groups(ORIGIN, policy_service=policy, fetch_json=server.get)
    assert server.calls == []
    policy.allowed = True
    group = await resolve_nodebb_group(f"{ORIGIN}/category/42", origin=ORIGIN, policy_service=policy, fetch_json=server.get)
    assert group.actor_id.endswith("/42")
    policy.allowed = False
    with pytest.raises(NodeBBDiscoveryError):
        await resolve_nodebb_group(group.actor_id, origin=ORIGIN, policy_service=policy, fetch_json=server.get)


def test_long_actor_choices_have_bounded_tokens_and_expire_without_silent_fallback(monkeypatch) -> None:
    """Loss of a process-local token cannot be mistaken for an authorized actor identity."""
    from src.nodebb_discovery import NodeBBGroup
    group = NodeBBGroup(
        actor_id="https://" + ("a" * 40) + "." + ("b" * 38) + ".example/category/42", inbox="https://example.test/inbox",
        name="T" * 150, handle="!discussion@example.test",
    )
    choice = nodebb_autocomplete_choices([group], "")[0]
    assert choice[1].startswith("nodebb-key:")
    assert len(choice[0]) <= 100 and len(choice[1]) <= 100
    assert decode_nodebb_choice(choice[1])[1] == group.actor_id
    key = choice[1].split(":", 1)[1]
    origin, actor, _ = _CHOICE_CACHE.entries[key]
    _CHOICE_CACHE.entries[key] = (origin, actor, 0)
    with pytest.raises(NodeBBDiscoveryError, match="expired"):
        decode_nodebb_choice(choice[1])


def test_nested_category_walk_is_bounded_and_does_not_escape_private_parent() -> None:
    """An attacker-controlled category tree cannot induce unlimited actor fetches."""
    tree = [{"cid": 1, "name": "Hidden", "private": True, "children": [{"cid": 2}]},
            {"cid": 3, "children": [{"cid": n} for n in range(4, 500)]}]
    rows = _flatten_categories(tree)
    assert {item["cid"] for item in rows}.isdisjoint({1, 2})
    assert len(rows) <= 96


@pytest.mark.asyncio
async def test_mixed_public_private_dns_answers_are_denied(monkeypatch) -> None:
    """DNS connector refuses both private-only and mixed public/private results."""
    import socket
    loop = __import__("asyncio").get_running_loop()

    async def getaddrinfo(host, port, *, family, type):
        """Model a poisoned DNS answer returned at the connection boundary."""
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]
    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
    with pytest.raises(NodeBBDiscoveryError, match="non-public"):
        await _PublicDNSResolver().resolve("nodebb.example", 443)

@pytest.mark.asyncio
async def test_autocomplete_confirmed_nodebb_never_falls_through_to_lemmy(monkeypatch) -> None:
    """Moderator searches proven NodeBB categories, including an empty result, without Lemmy calls."""
    from src import community_discovery as discovery
    server = NodeBBFixtureServer()
    policy = PublicPolicy()

    async def bridge_missing(origin: str):
        """Model a valid NodeBB deployment without Bridge discovery endpoint."""
        raise discovery.BridgeDiscoveryUnavailable(origin)

    async def get_groups(origin: str, *, policy_service, query: str):
        """Use actual NodeBB category/actor verification with fake external HTTP."""
        return await fetch_nodebb_groups(origin, policy_service=policy_service, fetch_json=server.get, query=query)

    class NoLemmy:
        """Reject fallback after NodeBB's API was confirmed."""

        def __init__(self, origin: str) -> None:
            """A Lemmy client would be an observable incorrect network action."""
            raise AssertionError("Lemmy fallback for confirmed NodeBB")

    monkeypatch.setattr(discovery, "fetch_nodebb_groups", get_groups)
    for query, expected in (("", 2), ("nested", 1), ("unrelated", 0)):
        codes: list[str] = []
        choices = await discovery.autocomplete_communities(
            None, instance_domain="nodebb.example", current=query,
            fetch_bridge_communities=bridge_missing, lemmy_client_cls=NoLemmy,
            policy_service=policy, on_diagnostic=codes.append,
        )
        assert len(choices) == expected
        assert all(value.startswith("nodebb:") for _, value in choices)
        assert codes == (["filter_no_match"] if expected == 0 else [])


@pytest.mark.asyncio
async def test_selected_nodebb_and_manual_url_reverify_before_follow(monkeypatch) -> None:
    """Choice and manual category URL produce one typed NodeBB target after current HTTP verification."""
    from src import community_discovery as discovery
    server = NodeBBFixtureServer()
    policy = PublicPolicy()

    async def bridge_missing(origin: str):
        """NodeBB does not advertise a Bridge-specific community API."""
        raise discovery.BridgeDiscoveryUnavailable(origin)

    async def probe(origin: str, *, policy_service):
        """Use the real bounded origin probe with injected fixture HTTP."""
        from src.nodebb_discovery import probe_nodebb_origin
        return await probe_nodebb_origin(origin, policy_service=policy_service, fetch_json=server.get)

    async def resolve(value: str, *, origin: str, policy_service):
        """Use the real WebFinger/category/Group validation for submitted values."""
        return await resolve_nodebb_group(value, origin=origin, policy_service=policy_service, fetch_json=server.get)

    monkeypatch.setattr(discovery, "probe_nodebb_origin", probe)
    monkeypatch.setattr(discovery, "resolve_nodebb_group", resolve)

    class NoLemmy:
        """Prevent a remote Lemmy fallback for a confirmed NodeBB Group."""

        def __init__(self, origin: str) -> None:
            """Any Lemmy lookup would be an inappropriate side effect."""
            raise AssertionError("Lemmy should not be called")

    for chosen in (f"nodebb:{ORIGIN}/category/42", f"{ORIGIN}/category/42/discussion", "!discussion@nodebb.example", "discussion"):
        selected = await discovery.resolve_selected_community(
            None, instance_domain="nodebb.example", community_value=chosen,
            fetch_bridge_communities=bridge_missing, lemmy_client_cls=NoLemmy,
            policy_service=policy,
        )
        assert selected.source == "remote_nodebb"
        assert selected.remote_software == "nodebb"
        assert selected.actor_id == f"{ORIGIN}/category/42"
        assert selected.numeric_id is None
    assert any("webfinger" in url for url, _ in server.calls)
    policy.allowed = False
    earlier_call_count = len(server.calls)
    with pytest.raises(discovery.CommunityResolutionError):
        await discovery.resolve_selected_community(
            None, instance_domain="nodebb.example",
            community_value=f"nodebb:{ORIGIN}/category/42",
            fetch_bridge_communities=bridge_missing, lemmy_client_cls=NoLemmy,
            policy_service=policy,
        )
    assert len(server.calls) == earlier_call_count


@pytest.mark.asyncio
async def test_non_nodebb_host_retains_existing_lemmy_fallback(monkeypatch) -> None:
    """A genuine 404 NodeBB API leaves existing Lemmy manual resolution functional."""
    from src import community_discovery as discovery

    async def bridge_missing(origin: str):
        """The remote host exposes no Bridge community discovery endpoint."""
        raise discovery.BridgeDiscoveryUnavailable(origin)

    async def absent_nodebb(origin: str, *, policy_service):
        """The remote host does not expose a NodeBB categories API."""
        raise NodeBBUnavailable()

    monkeypatch.setattr(discovery, "probe_nodebb_origin", absent_nodebb)

    class ExistingLemmy:
        """Return a conventional Lemmy numeric community resolution."""

        def __init__(self, origin: str) -> None:
            """Retain the resolved remote origin for contract validation."""
            self.origin = origin

        async def resolve_community(self, *, name: str):
            """Return the exact legacy Lemmy resolver response shape."""
            return {"name": name, "id": 3, "actor_id": f"{self.origin}/c/{name}"}

        async def close(self) -> None:
            """Implement the external client lifecycle contract."""
            return None

    # Existing direct-instance Lemmy lookup must stay available (the resolver
    # might choose a different client method than this minimal fake exposes).
    selected = await discovery.resolve_selected_community(
        None, instance_domain="lemmy.example", community_value="science",
        fetch_bridge_communities=bridge_missing, lemmy_client_cls=ExistingLemmy,
        policy_service=PublicPolicy(),
    )
    assert selected.source == "remote_lemmy"

@pytest.mark.asyncio
@pytest.mark.parametrize("http_status", [403, 503])
async def test_unidentified_nodebb_probe_failure_can_fall_back_to_lemmy(http_status: int) -> None:
    """An HTTP error on the *first* categories probe cannot prove a NodeBB origin."""
    from src.nodebb_discovery import NodeBBProbeInconclusive, probe_nodebb_origin

    async def denied_categories(url: str, accept: str) -> dict:
        """Model a non-NodeBB host answering the speculative categories path."""
        assert url == f"{ORIGIN}/api/categories"
        raise NodeBBDiscoveryError(
            f"speculative categories path returned HTTP {http_status}",
            reason_code="discovery_http_error",
        )

    for operation in (
        lambda: fetch_nodebb_groups(ORIGIN, policy_service=PublicPolicy(), fetch_json=denied_categories),
        lambda: probe_nodebb_origin(ORIGIN, policy_service=PublicPolicy(), fetch_json=denied_categories),
    ):
        with pytest.raises(NodeBBProbeInconclusive) as outcome:
            await operation()
        assert outcome.value.reason_code == "discovery_http_error"


@pytest.mark.asyncio
async def test_verified_nodebb_category_failure_is_not_unidentified_probe() -> None:
    """A category error after a valid NodeBB listing must not select a Lemmy identity."""
    from src.nodebb_discovery import NodeBBProbeInconclusive

    async def denied_detail(url: str, accept: str) -> dict:
        """Prove the host first, then fail while verifying a candidate Group."""
        if url == f"{ORIGIN}/api/categories":
            return {"categories": [{"cid": 42, "name": "Discussion", "handle": "discussion"}]}
        raise NodeBBDiscoveryError("category detail unavailable", reason_code="discovery_http_error")

    # Per-category errors are skipped, not reclassified as an unknown server.
    result = await fetch_nodebb_groups(ORIGIN, policy_service=PublicPolicy(), fetch_json=denied_detail)
    assert result == []
