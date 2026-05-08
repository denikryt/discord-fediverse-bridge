from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from discordops import run_operation_definition

from src.operations import ListSubscriptionsInput, list_subscriptions_operation
from tests.constants import LEMMY_EXAMPLE_DOMAIN


def test_list_subscriptions_operation_rejects_empty_state() -> None:
    # Empty-state logic lives in the operation so the command adapter can stay
    # focused on Discord rendering only.
    database = Mock()
    database.get_all_subscriptions.return_value = []

    result = run_operation_definition(
        list_subscriptions_operation,
        ListSubscriptionsInput(database=database),
    )

    assert result.applied is False
    assert result.reason == "subscriptions_exist"
    assert result.message == "No active subscriptions."


def test_list_subscriptions_operation_returns_embed_payload_data() -> None:
    # Success returns raw subscription rows in extra_kwargs so the command can
    # render an embed without recomputing policy decisions.
    subscriptions = [
        SimpleNamespace(
            discord_channel_id=111,
            lemmy_community_name="hackers",
            lemmy_community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
        ),
    ]
    database = Mock()
    database.get_all_subscriptions.return_value = subscriptions

    result = run_operation_definition(
        list_subscriptions_operation,
        ListSubscriptionsInput(database=database),
    )

    assert result.applied is True
    assert result.extra_kwargs == {"subscriptions": subscriptions}
    database.get_all_subscriptions.assert_called_once_with()
