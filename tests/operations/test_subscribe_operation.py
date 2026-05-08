from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from discordops import run_operation_definition
from sqlalchemy.exc import IntegrityError

from src.operations import SubscribeInput, subscribe_operation
from tests.constants import LEMMY_EXAMPLE_DOMAIN


def test_subscribe_operation_rejects_duplicates() -> None:
    # Duplicate channel mappings should short-circuit before any write attempt
    # and preserve the same moderator-facing message as the command adapter.
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database = Mock()
    database.get_subscription_by_channel.return_value = SimpleNamespace(
        lemmy_community_name="hackers",
        lemmy_community_actor_id=community_actor_url,
    )

    result = run_operation_definition(
        subscribe_operation,
        SubscribeInput(
            database=database,
            channel_id=123,
            channel_mention="<#123>",
            actor_id=community_actor_url,
            community_name="hackers",
            numeric_id=777,
        ),
    )

    assert result.applied is False
    assert result.reason == "channel_not_already_subscribed"
    assert result.message == (
        "Channel <#123> is already subscribed to **hackers**. Use `/unsubscribe-channel` first."
    )
    database.create_subscription.assert_not_called()


def test_subscribe_operation_creates_subscription_on_success() -> None:
    # Successful subscribe policy should pass the parsed community fields
    # straight into the repository write call.
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database = Mock()
    database.get_subscription_by_channel.return_value = None

    result = run_operation_definition(
        subscribe_operation,
        SubscribeInput(
            database=database,
            channel_id=123,
            channel_mention="<#123>",
            actor_id=community_actor_url,
            community_name="hackers",
            numeric_id=777,
        ),
    )

    assert result.applied is True
    assert result.message == "Subscribed <#123> to **hackers**."
    database.create_subscription.assert_called_once_with(
        discord_channel_id=123,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=777,
    )


def test_subscribe_operation_maps_integrity_error_to_rejection() -> None:
    # The DB uniqueness constraint remains the last safety net, so the
    # operation must translate an IntegrityError into a clean rejection result.
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database = Mock()
    database.get_subscription_by_channel.return_value = None
    database.create_subscription.side_effect = IntegrityError("stmt", "params", RuntimeError("duplicate"))

    result = run_operation_definition(
        subscribe_operation,
        SubscribeInput(
            database=database,
            channel_id=123,
            channel_mention="<#123>",
            actor_id=community_actor_url,
            community_name="hackers",
            numeric_id=777,
        ),
    )

    assert result.applied is False
    assert result.reason == "duplicate_subscription_integrity_error"
    assert result.message == "Channel <#123> already has a subscription."
