"""Tests for subscribe-channel community resolution without instance_domain."""

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
