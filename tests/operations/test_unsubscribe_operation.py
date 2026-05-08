from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from discordops import run_operation_definition

from src.operations import UnsubscribeInput, unsubscribe_operation
from tests_constants import LEMMY_EXAMPLE_DOMAIN


def test_unsubscribe_operation_rejects_missing_subscription() -> None:
    # Missing mappings should stop before the delete call so the repository
    # never sees a write attempt for a non-existent subscription.
    database = Mock()
    database.get_subscription_by_channel.return_value = None

    result = run_operation_definition(
        unsubscribe_operation,
        UnsubscribeInput(
            database=database,
            channel_id=123,
            channel_mention="<#123>",
        ),
    )

    assert result.applied is False
    assert result.reason == "channel_has_subscription"
    assert result.message == "Channel <#123> has no active subscription."
    database.delete_subscription.assert_not_called()


def test_unsubscribe_operation_deletes_existing_subscription() -> None:
    # Successful unsubscribe should use the cached row for the confirmation
    # label and then issue exactly one delete call.
    database = Mock()
    database.get_subscription_by_channel.return_value = SimpleNamespace(
        lemmy_community_name="hackers",
        lemmy_community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
    )
    database.delete_subscription.return_value = True

    result = run_operation_definition(
        unsubscribe_operation,
        UnsubscribeInput(
            database=database,
            channel_id=123,
            channel_mention="<#123>",
        ),
    )

    assert result.applied is True
    assert result.message == "Unsubscribed <#123> from **hackers**."
    database.get_subscription_by_channel.assert_called_once_with(123)
    database.delete_subscription.assert_called_once_with(123)
