"""Historical NodeBB replies must prove original root membership before Discord writes."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.community_sync.nodebb_backfill import fetch_verified_historical_root

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "nodebb"
GROUP = "https://nodebb.example/category/42"
ROOT = "https://nodebb.example/post/101"


def _fixture(name: str) -> dict:
    """Use the pinned upstream-shaped ActivityPub fixture from CP01."""
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.asyncio
async def test_pre_follow_root_present_in_verified_group_outbox() -> None:
    """An exact root in the Group collection is admitted; no guessing by context."""
    docs = {GROUP: _fixture("category_actor.json"),
            GROUP + "/outbox": _fixture("category_outbox_with_historical_root.json"),
            ROOT: _fixture("root_note.json")}
    async def fetch(url: str, _accept: str) -> dict:
        return docs[url]
    result = await fetch_verified_historical_root(
        root_id=ROOT, group_actor_id=GROUP, reply_context="https://nodebb.example/topic/5",
        policy=SimpleNamespace(), fetch=fetch,
    )
    assert result is not None and result["id"] == ROOT


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["wrong_group", "missing_root", "wrong_context", "wrong_actor", "private_outbox"])
async def test_unproven_root_never_authorized(change: str) -> None:
    """A claimed audience or unrelated collection entry cannot prove membership."""
    docs = {GROUP: _fixture("category_actor.json"),
            GROUP + "/outbox": _fixture("category_outbox_with_historical_root.json"),
            ROOT: _fixture("root_note.json")}
    if change == "wrong_group":
        docs[GROUP + "/outbox"]["orderedItems"][0]["actor"] = "https://nodebb.example/category/other"
    elif change == "missing_root":
        docs[GROUP + "/outbox"]["orderedItems"] = []
    elif change == "wrong_context":
        docs[ROOT]["context"] = "https://nodebb.example/topic/other"
    elif change == "wrong_actor":
        docs[GROUP]["id"] = "https://nodebb.example/category/other"
    else:
        docs[GROUP]["outbox"] = "http://127.0.0.1/outbox"
    async def fetch(url: str, _accept: str) -> dict:
        return docs[url]
    assert await fetch_verified_historical_root(
        root_id=ROOT, group_actor_id=GROUP, reply_context="https://nodebb.example/topic/5",
        policy=SimpleNamespace(), fetch=fetch,
    ) is None
