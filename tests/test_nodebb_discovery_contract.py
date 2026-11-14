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
    fetch_nodebb_candidates,
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
async def test_listing_builds_candidates_in_one_get_without_claiming_group_identity() -> None:
    """A moderator lists the public API tree without triggering per-actor network requests."""
    server = NodeBBFixtureServer()
    candidates = await fetch_nodebb_candidates(ORIGIN, policy_service=PublicPolicy(), fetch_json=server.get)
    # 45 is unreadable on the detail endpoint; 46 returns Person. They are
    # suggestions only, while private parents and explicit federation denials are excluded.
    assert [item.cid for item in candidates] == [42, 45, 46, 47]
    assert candidates[0].handle == "!discussion@nodebb.example"
    assert server.calls == [(f"{ORIGIN}/api/categories", "application/json")]
    choices = nodebb_autocomplete_choices(candidates, "DISCUSSION")
    assert len(choices) == 1
    assert choices[0][1] == f"nodebb:{ORIGIN}/category/42"
    assert all(len(name) <= 100 and len(value) <= 100 for name, value in choices)
    assert nodebb_autocomplete_choices(candidates, "https://nodebb.example/category/47")[0][1].endswith("/47")
    assert nodebb_autocomplete_choices(candidates, " ") == nodebb_autocomplete_choices(candidates, "")


@pytest.mark.asyncio
@pytest.mark.parametrize("section_has_group_actor", [True, False])
async def test_public_section_parent_keeps_its_federated_child_categories(
    section_has_group_actor: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NodeBB display section is not a visibility denial for itself or its children.

    A moderator lists the instance while Projects is a UI section containing
    three public federated categories; all verified children must appear even
    when the section itself does not expose a usable Group actor.
    """
    server = NodeBBFixtureServer()
    server.listing = {"categories": [
        {"cid": 50, "name": "Projects", "handle": "projects", "isSection": True,
         "children": [
             {"cid": 51, "name": "Blender:Basics Course", "handle": "blender"},
             {"cid": 52, "name": "NodeForge", "handle": "nodeforge"},
             {"cid": 53, "name": "АГОНЬ BMX", "handle": "bmx"},
         ]},
        {"cid": 60, "name": "Updates", "handle": "updates"},
        {"cid": 61, "name": "General Discussion", "handle": "general"},
        {"cid": 62, "name": "Blogs", "handle": "blogs"},
    ]}

    categories = [server.listing["categories"][0], *server.listing["categories"][0]["children"],
                  *server.listing["categories"][1:]]
    by_cid = {category["cid"]: category for category in categories}

    async def fetch(url: str, accept: str) -> dict:
        """Model public JSON and actor documents for every category/section."""
        if url == f"{ORIGIN}/api/categories":
            return server.listing
        if url.startswith(f"{ORIGIN}/api/category/"):
            cid = int(url.rsplit("/", 1)[1])
            return by_cid[cid]
        if url.startswith(f"{ORIGIN}/category/"):
            cid = int(url.rsplit("/", 1)[1])
            if cid == 50 and not section_has_group_actor:
                raise NodeBBUnavailable()
            category = by_cid[cid]
            return {**server.actor, "id": url, "type": "Group", "inbox": f"{url}/inbox",
                    "name": category["name"], "preferredUsername": category["handle"],
                    "url": f"{url}/{category['handle']}"}
        raise AssertionError(f"Unexpected external JSON fetch: {url}")

    candidates = await fetch_nodebb_candidates(
        ORIGIN, policy_service=PublicPolicy(), fetch_json=fetch,
    )
    found = [item.actor_url for item in candidates]
    expected = [f"{ORIGIN}/category/50"] + [
        f"{ORIGIN}/category/{cid}" for cid in (51, 52, 53, 60, 61, 62)
    ]
    assert found == expected
    assert len(nodebb_autocomplete_choices(candidates, "")) == len(expected)
    # Listing values are candidates, not Group proof; even a display section
    # without a real Group actor remains visible until selection validation.
    choices = nodebb_autocomplete_choices(candidates, "")
    assert len(choices) == len(expected)
    assert {value for _, value in choices} == {f"nodebb:{actor}" for actor in expected}
    assert any("NodeForge" in name for name, _ in choices)
    assert any("Blender:Basics Course" in name for name, _ in choices)

    # A moderator selecting instance_domain must receive these identities
    # through the real autocomplete router, not only the lower-level fetcher.
    from src import community_discovery as discovery

    async def bridge_missing(origin: str):
        """NodeBB has no Bridge-specific discovery endpoint."""
        raise discovery.BridgeDiscoveryUnavailable(origin)

    async def get_candidates(origin: str, *, policy_service):
        """Use the real one-request NodeBB listing with the controlled IO."""
        return await fetch_nodebb_candidates(
            origin, policy_service=policy_service, fetch_json=fetch,
        )

    class NoLemmy:
        """Ensure known NodeBB discovery never falls back to Lemmy."""

        def __init__(self, origin: str) -> None:
            """Any invocation would violate the recognized NodeBB route."""
            raise AssertionError(f"Unexpected Lemmy fallback for {origin}")

    monkeypatch.setattr(discovery, "fetch_nodebb_candidates", get_candidates)
    autocomplete = await discovery.autocomplete_communities(
        None, instance_domain="nodebb.example", current="",
        fetch_bridge_communities=bridge_missing, lemmy_client_cls=NoLemmy,
        policy_service=PublicPolicy(),
    )
    assert {value for _, value in autocomplete} == {value for _, value in choices}
    by_name = await discovery.autocomplete_communities(
        None, instance_domain="nodebb.example", current="NodeForge",
        fetch_bridge_communities=bridge_missing, lemmy_client_cls=NoLemmy,
        policy_service=PublicPolicy(),
    )
    assert [value for _, value in by_name] == [f"nodebb:{ORIGIN}/category/52"]
    # A section without a valid actor is still discoverable, but is rejected
    # during command submission before any Follow or DB change.
    if not section_has_group_actor:
        with pytest.raises(NodeBBUnavailable):
            await resolve_nodebb_group(
                f"{ORIGIN}/category/50", origin=ORIGIN,
                policy_service=PublicPolicy(), fetch_json=fetch,
            )
    else:
        verified = await resolve_nodebb_group(
            f"{ORIGIN}/category/50", origin=ORIGIN,
            policy_service=PublicPolicy(), fetch_json=fetch,
        )
        assert verified.inbox == f"{ORIGIN}/category/50/inbox"


@pytest.mark.asyncio
async def test_search_finds_later_category_without_verifying_first_25() -> None:
    """Category after the first 25 choices is searchable without extra HTTP calls."""
    server = NodeBBFixtureServer()
    server.listing = {"categories": [
        {"cid": number, "name": "Unrelated", "handle": "other"}
        for number in range(1, 70)
    ]}
    server.listing["categories"][-1] = {"cid": 69, "name": "Target", "handle": "target"}
    candidates = await fetch_nodebb_candidates(
        ORIGIN, policy_service=PublicPolicy(), fetch_json=server.get,
    )
    assert len(nodebb_autocomplete_choices(candidates, "")) == 25
    assert nodebb_autocomplete_choices(candidates, "TARGET") == [
        ("Target (target@nodebb.example)", f"nodebb:{ORIGIN}/category/69")
    ]
    assert server.calls == [(f"{ORIGIN}/api/categories", "application/json")]


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
        await fetch_nodebb_candidates(ORIGIN, policy_service=policy, fetch_json=server.get)
    assert server.calls == []
    policy.allowed = True
    group = await resolve_nodebb_group(f"{ORIGIN}/category/42", origin=ORIGIN, policy_service=policy, fetch_json=server.get)
    assert group.actor_id.endswith("/42")
    policy.allowed = False
    with pytest.raises(NodeBBDiscoveryError):
        await resolve_nodebb_group(group.actor_id, origin=ORIGIN, policy_service=policy, fetch_json=server.get)


def test_long_actor_choices_have_bounded_tokens_and_expire_without_silent_fallback(monkeypatch) -> None:
    """Loss of a process-local token cannot be mistaken for an authorized actor identity."""
    from src.nodebb_discovery import NodeBBCategoryCandidate
    actor_url = "https://" + ("a" * 40) + "." + ("b" * 38) + ".example/category/42"
    candidate = NodeBBCategoryCandidate(
        cid=42, actor_url=actor_url, name="T" * 150,
        handle="!discussion@example.test", slug="42-discussion",
    )
    choice = nodebb_autocomplete_choices([candidate], "")[0]
    assert choice[1].startswith("nodebb-key:")
    assert len(choice[0]) <= 100 and len(choice[1]) <= 100
    assert decode_nodebb_choice(choice[1])[1] == candidate.actor_url
    key = choice[1].split(":", 1)[1]
    origin, actor, _ = _CHOICE_CACHE.entries[key]
    _CHOICE_CACHE.entries[key] = (origin, actor, 0)
    with pytest.raises(NodeBBDiscoveryError, match="expired"):
        decode_nodebb_choice(choice[1])


def test_nested_category_walk_is_bounded_and_does_not_escape_private_parent() -> None:
    """A public nested tree is traversed without leaking hidden descendants."""
    tree = [{"cid": 1, "name": "Hidden", "private": True, "children": [{"cid": 2}]},
            {"cid": 3, "children": [{"cid": n} for n in range(4, 500)]}]
    rows = _flatten_categories(tree)
    assert {item["cid"] for item in rows}.isdisjoint({1, 2})
    assert len(rows) == 497


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

    async def get_candidates(origin: str, *, policy_service):
        """Use actual NodeBB listing with fake external HTTP."""
        return await fetch_nodebb_candidates(origin, policy_service=policy_service, fetch_json=server.get)

    class NoLemmy:
        """Reject fallback after NodeBB's API was confirmed."""

        def __init__(self, origin: str) -> None:
            """A Lemmy client would be an observable incorrect network action."""
            raise AssertionError("Lemmy fallback for confirmed NodeBB")

    monkeypatch.setattr(discovery, "fetch_nodebb_candidates", get_candidates)
    for query, expected in (("", 4), ("nested", 1), ("unrelated", 0)):
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
        lambda: fetch_nodebb_candidates(ORIGIN, policy_service=PublicPolicy(), fetch_json=denied_categories),
        lambda: probe_nodebb_origin(ORIGIN, policy_service=PublicPolicy(), fetch_json=denied_categories),
    ):
        with pytest.raises(NodeBBProbeInconclusive) as outcome:
            await operation()
        assert outcome.value.reason_code == "discovery_http_error"


@pytest.mark.asyncio
async def test_verified_nodebb_category_failure_is_not_unidentified_probe() -> None:
    """Category detail failure occurs at selection; autocomplete does no detail fetch."""
    async def denied_detail(url: str, accept: str) -> dict:
        """Prove listing first; detail is denied independently on submission."""
        if url == f"{ORIGIN}/api/categories":
            return {"categories": [{"cid": 42, "name": "Discussion", "handle": "discussion"}]}
        raise NodeBBDiscoveryError("category detail unavailable", reason_code="discovery_http_error")

    result = await fetch_nodebb_candidates(ORIGIN, policy_service=PublicPolicy(), fetch_json=denied_detail)
    assert [item.cid for item in result] == [42]
    with pytest.raises(NodeBBDiscoveryError, match="category detail unavailable"):
        await resolve_nodebb_group(
            result[0].actor_url, origin=ORIGIN,
            policy_service=PublicPolicy(), fetch_json=denied_detail,
        )


@pytest.mark.asyncio
async def test_mod_discord_autocomplete_lists_unverified_categories_with_one_nodebb_request(monkeypatch) -> None:
    """When moderator opens community, even unreadable/Person candidates appear after one listing GET."""
    from src import community_discovery as discovery
    server = NodeBBFixtureServer()

    async def bridge_missing(origin: str):
        """Stand in for absent Bridge discovery, not an ActivityPub identity."""
        raise discovery.BridgeDiscoveryUnavailable(origin)

    async def candidates(origin: str, *, policy_service):
        """Use the real listing conversion with the fixture HTTP boundary."""
        from src.nodebb_discovery import fetch_nodebb_candidates
        return await fetch_nodebb_candidates(origin, policy_service=policy_service, fetch_json=server.get)

    class NoLemmy:
        """Reject incorrect fallback from an identified NodeBB listing."""

        def __init__(self, origin: str) -> None:
            """Lemmy must not handle a confirmed NodeBB origin."""
            raise AssertionError(f"Lemmy fallback: {origin}")

    monkeypatch.setattr(discovery, "fetch_nodebb_candidates", candidates)
    for query, expected in (("", 4), (" ", 4), ("read-only", 1), ("DISCUSSION", 1)):
        server.calls.clear()
        suggestions = await discovery.autocomplete_communities(
            None, instance_domain="nodebb.example", current=query,
            fetch_bridge_communities=bridge_missing, lemmy_client_cls=NoLemmy,
            policy_service=PublicPolicy(),
        )
        assert len(suggestions) == expected
        assert server.calls == [(f"{ORIGIN}/api/categories", "application/json")]
    # Returned category is a candidate, even if its real actor is not a Group.
    assert suggestions[0][1] == f"nodebb:{ORIGIN}/category/42"


@pytest.mark.asyncio
async def test_unverified_autocomplete_selection_denied_before_follow_effects(monkeypatch) -> None:
    """After listing, the same command must reject unreadable/Person category before subscribe."""
    from src import community_discovery as discovery
    from src.nodebb_discovery import fetch_nodebb_candidates
    server = NodeBBFixtureServer()
    candidates = await fetch_nodebb_candidates(ORIGIN, policy_service=PublicPolicy(), fetch_json=server.get)
    options = nodebb_autocomplete_choices(candidates, "")
    assert len(options) == 4
    by_id = {value.rsplit("/", 1)[1]: value for _, value in options}

    async def probe(origin: str, *, policy_service):
        """Resolve with the actual NodeBB probe and controlled network transport."""
        from src.nodebb_discovery import probe_nodebb_origin
        return await probe_nodebb_origin(origin, policy_service=policy_service, fetch_json=server.get)

    async def resolve(value: str, *, origin: str, policy_service):
        """Run the verified category/read/actor checks used before SubscribeInput."""
        return await resolve_nodebb_group(value, origin=origin, policy_service=policy_service, fetch_json=server.get)

    monkeypatch.setattr(discovery, "probe_nodebb_origin", probe)
    monkeypatch.setattr(discovery, "resolve_nodebb_group", resolve)
    for invalid in ("45", "46"):
        with pytest.raises(discovery.CommunityResolutionError):
            await discovery.resolve_selected_community(
                None, instance_domain="nodebb.example", community_value=by_id[invalid],
                fetch_bridge_communities=None, lemmy_client_cls=None, policy_service=PublicPolicy(),
            )
    selected = await discovery.resolve_selected_community(
        None, instance_domain="nodebb.example", community_value=by_id["42"],
        fetch_bridge_communities=None, lemmy_client_cls=None, policy_service=PublicPolicy(),
    )
    assert selected.source == "remote_nodebb"
    assert selected.actor_id == f"{ORIGIN}/category/42"
    assert selected.remote_software == "nodebb"
    assert any(url == f"{ORIGIN}/category/42" for url, _ in server.calls)


def test_listing_bounds_cannot_silently_hide_categories() -> None:
    """An oversized/deep tree reports failure instead of silently dropping searchable children."""
    from src.nodebb_discovery import MAX_CATEGORY_NODES, MAX_CATEGORY_DEPTH
    large = [{"cid": cid, "name": f"Category {cid}"} for cid in range(1, MAX_CATEGORY_NODES + 2)]
    with pytest.raises(NodeBBDiscoveryError, match="limit"):
        _flatten_categories(large)
    nested: dict = {"cid": MAX_CATEGORY_DEPTH + 2, "name": "Deep"}
    for cid in range(MAX_CATEGORY_DEPTH + 1, 0, -1):
        nested = {"cid": cid, "name": "Level", "children": [nested]}
    with pytest.raises(NodeBBDiscoveryError, match="depth"):
        _flatten_categories([nested])

@pytest.mark.asyncio
async def test_full_handle_and_duplicate_parent_do_not_hide_valid_category_children() -> None:
    """List navigation preserves nested categories and does not duplicate an API-provided host."""
    server = NodeBBFixtureServer()
    server.listing = {"categories": [
        {"cid": 1, "name": "Parent", "handle": "!parent@nodebb.example"},
        {"cid": 1, "name": "Repeated", "children": [
            {"cid": 3, "name": "Child", "handle": "!child@nodebb.example"},
        ]},
        {"name": "Display wrapper", "children": [
            {"cid": 4, "name": "Second child", "handle": "second"},
        ]},
    ]}
    candidates = await fetch_nodebb_candidates(ORIGIN, policy_service=PublicPolicy(), fetch_json=server.get)
    assert [item.cid for item in candidates] == [1, 3, 4]
    assert [item.handle for item in candidates] == [
        "!parent@nodebb.example", "!child@nodebb.example", "!second@nodebb.example",
    ]
    assert server.calls == [(f"{ORIGIN}/api/categories", "application/json")]

@pytest.mark.asyncio
async def test_verified_candidate_allows_one_durable_follow_binding_after_submit(tmp_path, monkeypatch) -> None:
    """Moderator action: invalid choices leave SQLite empty; a verified Group can reserve Follow.

    The gateway is an external boundary: this checks the real NodeBB resolver
    and database reservation, not HTTP signature/delivery of the Follow itself.
    """
    from src import community_discovery as discovery
    from src.db import Database
    from src.db.repositories.bridge_actor_follows import BridgeActorFollowRepository
    from src.models import BridgeActorFollow, ChannelCommunitySubscription

    server = NodeBBFixtureServer()
    candidates = await fetch_nodebb_candidates(ORIGIN, policy_service=PublicPolicy(), fetch_json=server.get)

    async def probe(origin: str, *, policy_service):
        """Use real HTTP-boundary probe with the fake remote endpoint."""
        from src.nodebb_discovery import probe_nodebb_origin
        return await probe_nodebb_origin(origin, policy_service=policy_service, fetch_json=server.get)

    async def resolve(value: str, *, origin: str, policy_service):
        """Always verify current category permissions and Group actor."""
        return await resolve_nodebb_group(value, origin=origin, policy_service=policy_service, fetch_json=server.get)

    monkeypatch.setattr(discovery, "probe_nodebb_origin", probe)
    monkeypatch.setattr(discovery, "resolve_nodebb_group", resolve)
    db = Database(f"sqlite:///{tmp_path / 'nodebb-candidate-follow.sqlite'}")
    db.create_all()
    try:
        for candidate in candidates:
            selected_value = f"nodebb:{candidate.actor_url}"
            if candidate.cid in {45, 46}:
                with pytest.raises(discovery.CommunityResolutionError):
                    await discovery.resolve_selected_community(
                        None, instance_domain="nodebb.example", community_value=selected_value,
                        fetch_bridge_communities=None, lemmy_client_cls=None,
                        policy_service=PublicPolicy(),
                    )
        with db.session() as session:
            assert session.query(BridgeActorFollow).count() == 0
            assert session.query(ChannelCommunitySubscription).count() == 0

        selected = await discovery.resolve_selected_community(
            None, instance_domain="nodebb.example",
            community_value=f"nodebb:{ORIGIN}/category/42",
            fetch_bridge_communities=None, lemmy_client_cls=None,
            policy_service=PublicPolicy(),
        )
        assert selected.source == "remote_nodebb" and selected.numeric_id is None
        with db.immediate_session() as session:
            follow, must_send = BridgeActorFollowRepository.reserve_with_binding(
                session,
                community_actor_id=selected.actor_id,
                community_inbox_url=f"{ORIGIN}/category/42/inbox",
                follow_activity_id="https://bridge.example/activities/follow/one",
                remote_software_profile=selected.remote_software,
                discord_channel_id=100,
                discord_guild_id=5,
                community_name=selected.name,
                numeric_id=selected.numeric_id,
                community_handle=selected.handle,
                initiated_by_discord_user_id="10",
            )
            assert must_send and follow.follow_dispatch_state == "prepared"
        with db.session() as session:
            assert session.query(BridgeActorFollow).count() == 1
            assert session.query(ChannelCommunitySubscription).count() == 1
    finally:
        db.close()
