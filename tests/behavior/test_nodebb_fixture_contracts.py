"""Check the source-pinned NodeBB fixtures before integrating runtime support."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_FIXTURES = Path(__file__).parents[1] / "fixtures/nodebb"


def _fixture(name: str) -> dict[str, object]:
    """Read one checked-in scenario payload without contacting the network."""
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def test_pinned_category_actor_uses_canonical_numeric_id_not_public_slug() -> None:
    """A category UI slug must not be substituted for its signed Group actor ID."""
    actor = _fixture("category_actor.json")
    assert actor["type"] == "Group"
    assert actor["id"] == "https://nodebb.example/category/42"
    assert actor["url"] != actor["id"]
    assert actor["inbox"] == actor["id"] + "/inbox"
    assert actor["outbox"] == actor["id"] + "/outbox"


def test_pinned_group_announce_can_reference_uri_or_nested_create() -> None:
    """Envelope ID and object identity must remain distinct on both valid forms."""
    root = _fixture("root_note.json")
    reply = _fixture("reply_note.json")
    announcement = _fixture("announce_root_uri.json")
    nested = _fixture("announce_nested_create.json")
    assert announcement["type"] == nested["type"] == "Announce"
    assert announcement["object"] == root["id"]
    assert nested["object"]["type"] == "Create"
    assert nested["object"]["object"]["id"] == root["id"]
    assert nested["id"] != nested["object"]["id"]
    assert root["inReplyTo"] is None
    assert reply["inReplyTo"] == root["id"]
    assert root["context"] == reply["context"]
    assert root["audience"] == reply["audience"]


@pytest.mark.parametrize("target,member", [
    ("https://nodebb.example/post/101", True),
    ("https://nodebb.example/post/202", False),
])
def test_pinned_category_outbox_contains_only_exact_historical_root(
    target: str, member: bool
) -> None:
    """Context or category URL alone cannot prove historical Group membership."""
    outbox = _fixture("category_outbox_with_historical_root.json")
    assert outbox["id"] == "https://nodebb.example/category/42/outbox"
    assert all(item["actor"] == "https://nodebb.example/category/42" for item in outbox["orderedItems"])
    # This only verifies fixture data; the trusted proof must be added at CP13.
    root_ids = {item["object"]["object"]["id"] for item in outbox["orderedItems"]}
    assert (target in root_ids) is member
