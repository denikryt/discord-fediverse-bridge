"""Executable identity/discovery contract cases."""
from __future__ import annotations
import pytest
from src.community_discovery import BridgeDiscoveryUnavailable, CommunityResolutionError, resolve_selected_community
from src.community_labels import community_relay_label
from src.fediverse_identity import InvalidRemoteActorHandle, extract_remote_actor_handle_from_actor_url, normalize_remote_actor_handle
from support.identity_discovery_contracts import IDENTITY_DISCOVERY_CASES, IdentityDiscoveryCase

class FakeLemmyClient:
    def __init__(self, origin: str) -> None: self.origin = origin
    async def resolve_community(self, *, name: str) -> dict[str, object]:
        slug = name.rstrip('/').split('/')[-1]
        return {"actor_id": f"{self.origin}/c/{slug}", "name": slug, "id": 42}
    async def close(self) -> None: return None

async def _no_bridge(origin: str):
    raise BridgeDiscoveryUnavailable(origin)

@pytest.mark.parametrize("case", IDENTITY_DISCOVERY_CASES, ids=lambda case: case.id)
@pytest.mark.asyncio
async def test_identity_discovery_contract(case: IdentityDiscoveryCase) -> None:
    if case.action == "normalize_handle":
        if case.expected.error_contains:
            with pytest.raises(InvalidRemoteActorHandle): normalize_remote_actor_handle(case.raw)
        else: assert normalize_remote_actor_handle(case.raw) == case.expected.value
    elif case.action == "extract_handle":
        assert extract_remote_actor_handle_from_actor_url(case.raw) == case.expected.value
    elif case.action == "resolve_community":
        if case.expected.error_contains:
            with pytest.raises(CommunityResolutionError) as error:
                await resolve_selected_community(None, instance_domain=None, community_value=case.raw, fetch_bridge_communities=_no_bridge, lemmy_client_cls=FakeLemmyClient)
            assert case.expected.error_contains in str(error.value)
        else:
            resolved = await resolve_selected_community(None, instance_domain=None, community_value=case.raw, fetch_bridge_communities=_no_bridge, lemmy_client_cls=FakeLemmyClient)
            assert resolved.actor_id == case.expected.value
            assert resolved.source == case.expected.source
    else:
        if case.raw.startswith("!"):
            actual = community_relay_label(actor_id="https://lemmy.example/c/hackers", name="hackers", handle=case.raw)
        else:
            actual = community_relay_label(actor_id=case.raw, name="Technology", handle=None)
        assert actual == case.expected.value
