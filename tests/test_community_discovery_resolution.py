"""Tests for subscribe-community community resolution without instance_domain."""

from __future__ import annotations

import pytest

from src.community_discovery import CommunityResolutionError, resolve_selected_community


class FakeLemmyClient:
    """Minimal Lemmy client fake for remote-community resolution tests."""

    def __init__(self, origin: str) -> None:
        """Capture the origin inferred by the resolver."""
        self.origin = origin

    async def resolve_community(self, *, name: str) -> dict[str, object]:
        """Return one resolved remote community for the requested slug/name."""
        slug = name.rstrip("/").split("/")[-1]
        return {"actor_id": f"{self.origin}/c/{slug}", "name": slug, "id": 42}

    async def close(self) -> None:
        """Match the cleanup method used by the resolver."""
        return None


async def _no_bridge(origin: str):
    """Force resolution through the Lemmy fallback path."""
    from src.community_discovery import BridgeDiscoveryUnavailable

    raise BridgeDiscoveryUnavailable(origin)


@pytest.mark.asyncio
async def test_direct_actor_url_resolves_without_instance_domain() -> None:
    """Lemmyverse actor URLs carry their own origin and do not need instance_domain."""
    resolved = await resolve_selected_community(
        None,
        instance_domain=None,
        community_value="https://lemmy.world/c/technology",
        fetch_bridge_communities=_no_bridge,
        lemmy_client_cls=FakeLemmyClient,
    )

    assert resolved.source == "remote_lemmy"
    assert resolved.actor_id == "https://lemmy.world/c/technology"
    assert resolved.name == "technology"
    assert resolved.numeric_id == 42


@pytest.mark.asyncio
async def test_fediverse_handle_resolves_without_instance_domain() -> None:
    """Fediverse handles infer the remote origin from the handle host."""
    resolved = await resolve_selected_community(
        None,
        instance_domain=None,
        community_value="!technology@lemmy.world",
        fetch_bridge_communities=_no_bridge,
        lemmy_client_cls=FakeLemmyClient,
    )

    assert resolved.actor_id == "https://lemmy.world/c/technology"
    assert resolved.handle == "!technology@lemmy.world"


@pytest.mark.asyncio
async def test_plain_name_without_instance_domain_is_ambiguous() -> None:
    """Plain names require an instance because global names are not unique."""
    with pytest.raises(CommunityResolutionError) as error:
        await resolve_selected_community(
            None,
            instance_domain=None,
            community_value="technology",
            fetch_bridge_communities=_no_bridge,
            lemmy_client_cls=FakeLemmyClient,
        )

    assert "Select a community from autocomplete" in str(error.value)


@pytest.mark.asyncio
async def test_encoded_value_resolves_without_instance_domain() -> None:
    """Legacy encoded autocomplete payloads remain self-contained."""
    resolved = await resolve_selected_community(
        None,
        instance_domain=None,
        community_value="lemmy:https://lemmy.world/c/technology|technology|42",
        fetch_bridge_communities=_no_bridge,
        lemmy_client_cls=FakeLemmyClient,
    )

    assert resolved.actor_id == "https://lemmy.world/c/technology"
    assert resolved.numeric_id == 42


class FakeAutocompleteLemmyClient:
    """Lemmy client fake returning community-view rows for autocomplete tests."""

    def __init__(self, origin: str) -> None:
        """Store the normalized origin requested by autocomplete."""
        self.origin = origin
        self.closed = False

    async def list_communities(
        self, *, limit: int, type_: str
    ) -> list[dict[str, object]]:
        """Return one direct-instance community with monthly activity counts."""
        assert limit == 50
        assert type_ == "Local"
        return [
            {
                "community": {
                    "name": "quiet",
                    "title": "Quiet Community",
                    "actor_id": f"{self.origin}/c/quiet",
                    "id": 41,
                },
                "counts": {"users_active_month": 12},
            },
            {
                "community": {
                    "name": "worldnews",
                    "title": "World News",
                    "actor_id": f"{self.origin}/c/worldnews",
                    "id": 42,
                },
                "counts": {"users_active_month": 456},
            },
        ]

    async def close(self) -> None:
        """Record cleanup so the fake matches the real client contract."""
        self.closed = True


@pytest.mark.asyncio
async def test_direct_instance_autocomplete_labels_include_handle_and_monthly_activity() -> (
    None
):
    """Direct-instance autocomplete labels should match global handle context."""
    from src.community_discovery import autocomplete_communities

    choices = await autocomplete_communities(
        None,
        instance_domain="lemmy.world",
        current="",
        fetch_bridge_communities=_no_bridge,
        lemmy_client_cls=FakeAutocompleteLemmyClient,
    )

    assert choices == [
        (
            "World News (worldnews@lemmy.world · 456 active/mo)",
            "lemmy:https://lemmy.world/c/worldnews|worldnews|42",
        ),
        (
            "Quiet Community (quiet@lemmy.world · 12 active/mo)",
            "lemmy:https://lemmy.world/c/quiet|quiet|41",
        ),
    ]

@pytest.mark.asyncio
@pytest.mark.parametrize("http_status", [403, 503])
async def test_manual_lemmy_handle_falls_back_after_unidentified_nodebb_probe(
    http_status: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual Lemmy references remain usable when a speculative NodeBB probe fails."""
    from types import SimpleNamespace
    from src import nodebb_discovery
    from src.nodebb_discovery import NodeBBDiscoveryError

    class FailedNodeBBProbe:
        """Model a non-NodeBB origin failing its NodeBB-specific API endpoint."""

        def __init__(self, origin: str, policy: object) -> None:
            """Use the actual secure probe constructor arguments."""
            assert origin == "https://lemmy.world"

        async def __call__(self, url: str, accept: str) -> dict:
            """Fail before any NodeBB Group identity is observed."""
            assert url == "https://lemmy.world/api/categories"
            raise NodeBBDiscoveryError(f"HTTP {http_status}", reason_code="discovery_http_error")

    monkeypatch.setattr(nodebb_discovery, "SecureNodeBBJSON", FailedNodeBBProbe)
    policy = SimpleNamespace(federation_decision=lambda origin: SimpleNamespace(allowed=True))
    result = await resolve_selected_community(
        None, instance_domain=None, community_value="!technology@lemmy.world",
        fetch_bridge_communities=_no_bridge, lemmy_client_cls=FakeLemmyClient,
        policy_service=policy,
    )
    assert result.source == "remote_lemmy"
    assert result.actor_id == "https://lemmy.world/c/technology"

    # Explicit NodeBB selections cannot be reinterpreted as a Lemmy target.
    with pytest.raises(CommunityResolutionError) as error:
        await resolve_selected_community(
            None, instance_domain=None,
            community_value="nodebb:https://lemmy.world/category/42",
            fetch_bridge_communities=_no_bridge, lemmy_client_cls=FakeLemmyClient,
            policy_service=policy,
        )
    assert error.value.reason_code == "discovery_http_error"

@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", ["http_error", "not_nodebb"])
async def test_explicit_category_url_never_downgrades_to_an_unrelated_lemmy_community(
    unavailable: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manually pasted /category/42 URL cannot silently become Lemmy /c/42."""
    from types import SimpleNamespace
    from src import nodebb_discovery
    from src.nodebb_discovery import NodeBBDiscoveryError, NodeBBUnavailable

    class MissingNodeBBAPI:
        """Model failure to prove whether the pasted NodeBB category exists."""

        def __init__(self, origin: str, policy: object) -> None:
            """Bind the expected origin."""
            assert origin == "https://lemmy.world"

        async def __call__(self, url: str, accept: str) -> dict:
            """Fail the first NodeBB categories probe."""
            assert url.endswith("/api/categories")
            if unavailable == "not_nodebb":
                raise NodeBBUnavailable()
            raise NodeBBDiscoveryError("HTTP 503", reason_code="discovery_http_error")

    monkeypatch.setattr(nodebb_discovery, "SecureNodeBBJSON", MissingNodeBBAPI)
    policy = SimpleNamespace(federation_decision=lambda origin: SimpleNamespace(allowed=True))
    with pytest.raises(CommunityResolutionError):
        await resolve_selected_community(
            None, instance_domain=None,
            community_value="https://lemmy.world/category/42",
            fetch_bridge_communities=_no_bridge, lemmy_client_cls=FakeLemmyClient,
            policy_service=policy,
        )
