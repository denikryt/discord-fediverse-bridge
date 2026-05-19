"""App-specific operation contracts for Discord command policy."""

from .create_community import (
    CreateCommunityInput,
    CreateCommunityResult,
    create_community_operation,
)
from .list_subscriptions import ListSubscriptionsInput, list_subscriptions_operation
from .subscribe import SubscribeInput, subscribe_operation
from .unsubscribe import UnsubscribeInput, unsubscribe_operation

__all__ = [
    "CreateCommunityInput",
    "CreateCommunityResult",
    "ListSubscriptionsInput",
    "SubscribeInput",
    "UnsubscribeInput",
    "create_community_operation",
    "list_subscriptions_operation",
    "subscribe_operation",
    "unsubscribe_operation",
]
