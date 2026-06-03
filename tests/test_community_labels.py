"""Regression tests for moderator-facing community label formatting."""

from src.community_labels import community_relay_label


def test_community_relay_label_prefers_stored_handle_without_bang() -> None:
    """Stored Lemmy handles should render as slug@instance labels."""
    assert (
        community_relay_label(
            actor_id="https://lemmy.example/c/hackers",
            name="hackers",
            handle="!hackers@lemmy.example",
        )
        == "hackers@lemmy.example"
    )


def test_community_relay_label_extracts_actor_url_slug_and_host() -> None:
    """Raw actor URLs should still produce compact relay labels."""
    assert (
        community_relay_label(
            actor_id="https://lemmy.world/c/technology",
            name="Technology",
            handle=None,
        )
        == "technology@lemmy.world"
    )
