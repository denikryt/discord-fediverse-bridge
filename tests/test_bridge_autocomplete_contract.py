"""Observable Bridge autocomplete and safe diagnostics through the discovery entrypoint.

The fake external HTTP and Lemmy edges preserve real query, choice encoding, and
fallback behavior without requiring a live remote instance or Discord client.
"""

from __future__ import annotations

import httpx
import pytest

from src.community_discovery import (
    BridgeCommunitySummary,
    BridgeDiscoveryUnavailable,
    CommunityResolutionError,
    _parse_encoded_community_value,
    autocomplete_communities,
)


class UnexpectedLemmyClient:
    """Catch accidental fallback from an authenticated Bridge discovery result."""

    def __init__(self, origin: str) -> None:
        """Reject any Lemmy client for a confirmed Bridge origin."""
        raise AssertionError(f"unexpected Lemmy fallback: {origin}")


class LemmyClient:
    """Record the observed Lemmy fallback without performing network calls."""

    calls: list[str] = []

    def __init__(self, origin: str) -> None:
        """Preserve the origin to check that fallback remains unchanged."""
        self.origin = origin
        self.calls.append(origin)

    async def list_communities(self, *, limit: int, type_: str):
        """Return a single standard Lemmy listing."""
        assert (limit, type_) == (50, "Local")
        return [{"community": {"name": "lemmy", "title": "Lemmy", "actor_id": f"{self.origin}/c/lemmy", "id": 91}}]

    async def close(self) -> None:
        """Honor the existing discovery client's cleanup contract."""
        return None


def _summary(*, actor: str = "https://bridge.test/communities/science", name: str = "Science") -> BridgeCommunitySummary:
    """Build one discovery response with all actual Bridge identity fields."""
    return BridgeCommunitySummary(
        id=17,
        slug="science",
        name=name,
        title="Research and exploration",
        description=None,
        actor_id=actor,
        alternate_actor_id="https://bridge.test/c/science",
        handle="!science@bridge.test",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "search",
    [
        "", "SCIENCE", "!SCIENCE@BRIDGE.TEST", "science@bridge.test",
        "https://bridge.test/communities/science", "/communities/science",
        "https://bridge.test/c/science", "/c/science", "RESEARCH",
    ],
)
async def test_bridge_remote_autocomplete_finds_verified_summary_fields(search: str) -> None:
    """Moderator search by handle, alias, canonical ID or name preserves typed remote choice."""
    async def fetch(origin: str):
        """Return an authenticated Bridge discovery list from the requested host."""
        assert origin == "https://bridge.test"
        return [_summary()]

    choices = await autocomplete_communities(
        None,
        instance_domain="bridge.test",
        current=search,
        fetch_bridge_communities=fetch,
        lemmy_client_cls=UnexpectedLemmyClient,
    )
    assert len(choices) == 1
    assert choices[0][1].startswith("bridge-remote:")
    assert "|17" not in choices[0][1]  # Remote database IDs must never become local IDs.
    resolved = _parse_encoded_community_value(choices[0][1])
    assert resolved is not None and resolved.source == "remote_bridge"
    assert resolved.actor_id == "https://bridge.test/communities/science"
    assert resolved.numeric_id is None


@pytest.mark.asyncio
async def test_bridge_discovery_negative_filter_and_empty_list_emit_one_safe_reason() -> None:
    """Empty results are classified without logging payloads or silently falling to Lemmy."""
    reasons: list[str] = []

    async def fetch(origin: str):
        """Return a confirmed Bridge with one unrelated group."""
        return [_summary()]

    choices = await autocomplete_communities(
        None, instance_domain="bridge.test", current="nonexistent",
        fetch_bridge_communities=fetch, lemmy_client_cls=UnexpectedLemmyClient,
        on_diagnostic=reasons.append,
    )
    assert choices == [] and reasons == ["filter_no_match"]
    reasons.clear()

    async def fetch_empty(origin: str):
        """Return a valid Bridge identity with no public groups."""
        return []

    choices = await autocomplete_communities(
        None, instance_domain="bridge.test", current="",
        fetch_bridge_communities=fetch_empty, lemmy_client_cls=UnexpectedLemmyClient,
        on_diagnostic=reasons.append,
    )
    assert choices == [] and reasons == ["no_public_groups"]


@pytest.mark.asyncio
async def test_bridge_autocomplete_obeys_both_discord_limits_without_truncating_actor_id() -> None:
    """Long titles fit; unencodable canonical actors are omitted, never silently truncated."""
    origins = [
        _summary(name="X" * 180),
        _summary(actor="https://bridge.test/communities/" + "a" * 130),
    ]
    reasons: list[str] = []

    async def fetch(origin: str):
        """Return two public groups with potentially oversized Discord choices."""
        return origins

    choices = await autocomplete_communities(
        None, instance_domain="bridge.test", current="",
        fetch_bridge_communities=fetch, lemmy_client_cls=UnexpectedLemmyClient,
        on_diagnostic=reasons.append,
    )
    assert len(choices) == 1
    assert all(len(name) <= 100 and len(value) <= 100 for name, value in choices)
    assert _parse_encoded_community_value(choices[0][1]).actor_id == origins[0].actor_id
    assert reasons == []


@pytest.mark.asyncio
async def test_bridge_remote_discovery_errors_do_not_expose_payload_or_change_lemmy_fallback() -> None:
    """A missing Bridge endpoint still uses Lemmy, while failed fallback is diagnosed safely."""
    LemmyClient.calls.clear()
    reasons: list[str] = []

    async def missing(origin: str):
        """Report an origin with no Bridge endpoint."""
        raise BridgeDiscoveryUnavailable(origin)

    choices = await autocomplete_communities(
        None, instance_domain="lemmy.test", current="lemmy",
        fetch_bridge_communities=missing, lemmy_client_cls=LemmyClient,
        on_diagnostic=reasons.append,
    )
    assert len(choices) == 1
    assert choices[0][1].startswith("lemmy:")
    assert LemmyClient.calls == ["https://lemmy.test"] and reasons == []

    async def timeout(origin: str):
        """Surface a timeout without its URL/request payload."""
        raise CommunityResolutionError("secret bearer token", reason_code="discovery_timeout")

    class FailedLemmyClient(LemmyClient):
        """Model a failed Lemmy fallback after failed Bridge discovery."""

        async def list_communities(self, *, limit: int, type_: str):
            """Raise a transport failure from the second discovery provider."""
            raise httpx.ReadTimeout("secret private body")

    reasons.clear()
    choices = await autocomplete_communities(
        None, instance_domain="lemmy.test", current="",
        fetch_bridge_communities=timeout, lemmy_client_cls=FailedLemmyClient,
        on_diagnostic=reasons.append,
    )
    assert choices == [] and reasons == ["discovery_timeout"]

@pytest.mark.asyncio
async def test_bridge_local_choices_keep_local_ids_and_limit_to_25() -> None:
    """A same-instance Bridge list returns at most 25 stable local ID selections."""
    from types import SimpleNamespace

    settings = SimpleNamespace(
        normalized_public_bridge_base_url="https://bridge.test",
        normalized_fedify_origin="https://bridge.test",
    )
    summaries = [
        BridgeCommunitySummary(
            id=index, slug=f"community-{index}", name=f"community-{index}",
            title=f"Community {index}", description=None,
            actor_id=f"https://bridge.test/communities/community-{index}",
            alternate_actor_id=f"https://bridge.test/c/community-{index}",
            handle=f"!community-{index}@bridge.test",
        ) for index in range(1, 31)
    ]

    async def fetch(origin: str):
        """Return thirty owned public Group identities."""
        assert origin == "https://bridge.test"
        return summaries

    choices = await autocomplete_communities(
        settings, instance_domain="bridge.test", current="",
        fetch_bridge_communities=fetch, lemmy_client_cls=UnexpectedLemmyClient,
    )
    assert len(choices) == 25
    assert all(len(name) <= 100 and len(value) <= 100 for name, value in choices)
    assert all(value.startswith("bridge-local:") for _, value in choices)
    for index, (_, value) in enumerate(choices, start=1):
        resolved = _parse_encoded_community_value(value)
        assert resolved is not None
        assert resolved.source == "local_bridge"
        assert resolved.local_community_id == index


@pytest.mark.asyncio
async def test_bridge_malformed_discovery_failure_does_not_return_unverified_choices() -> None:
    """A malformed summary cannot be selected or used as a Lemmy identity locally."""
    from types import SimpleNamespace

    settings = SimpleNamespace(
        normalized_public_bridge_base_url="https://bridge.test",
        normalized_fedify_origin="https://bridge.test",
    )
    reasons: list[str] = []

    async def malformed(origin: str):
        """Reject the response at the discovery boundary before choice construction."""
        raise CommunityResolutionError("private payload", reason_code="malformed_response")

    choices = await autocomplete_communities(
        settings, instance_domain="bridge.test", current="",
        fetch_bridge_communities=malformed, lemmy_client_cls=UnexpectedLemmyClient,
        on_diagnostic=reasons.append,
    )
    assert choices == []
    assert reasons == ["malformed_response"]

@pytest.mark.asyncio
@pytest.mark.parametrize("http_status", [403, 503])
async def test_ambiguous_nodebb_categories_http_failure_does_not_hide_healthy_lemmy(
    http_status: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A working Lemmy listing wins after a speculative NodeBB probe gets 403/503."""
    from types import SimpleNamespace
    from src import nodebb_discovery
    from src.nodebb_discovery import NodeBBDiscoveryError

    class CategoriesHTTPFailure:
        """Model a candidate origin with only an error at /api/categories."""

        def __init__(self, origin: str, policy: object) -> None:
            """Accept the production adapter's constructor arguments."""
            assert origin == "https://lemmy.test"

        async def __call__(self, url: str, accept: str) -> dict:
            """Return the observed speculative HTTP failure, never Group evidence."""
            assert url == "https://lemmy.test/api/categories"
            raise NodeBBDiscoveryError(
                f"HTTP {http_status}", reason_code="discovery_http_error",
            )

    monkeypatch.setattr(nodebb_discovery, "SecureNodeBBJSON", CategoriesHTTPFailure)
    LemmyClient.calls.clear()
    diagnostics: list[str] = []

    async def no_bridge(origin: str) -> list:
        """Rule out the already supported remote-Bridge discovery path."""
        raise BridgeDiscoveryUnavailable(origin)

    choices = await autocomplete_communities(
        None, instance_domain="lemmy.test", current="lemmy",
        fetch_bridge_communities=no_bridge, lemmy_client_cls=LemmyClient,
        policy_service=SimpleNamespace(federation_decision=lambda origin: SimpleNamespace(allowed=True)),
        on_diagnostic=diagnostics.append,
    )
    assert len(choices) == 1 and choices[0][1].startswith("lemmy:")
    assert LemmyClient.calls == ["https://lemmy.test"]
    assert diagnostics == []
