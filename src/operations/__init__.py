"""App-specific operation contracts for Discord command policy."""

from .create_community import (
    CreateCommunityInput,
    CreateCommunityResult,
    create_community_operation,
)
from .list_subscriptions import ListSubscriptionsInput, list_subscriptions_operation
from .subscribe_local_community import (
    SubscribeLocalCommunityInput,
    subscribe_local_community_operation,
)
from .subscribe import SubscribeInput, subscribe_operation
from .unsubscribe_local_community import (
    UnsubscribeLocalCommunityInput,
    unsubscribe_local_community_operation,
)
from .unsubscribe import UnsubscribeInput, unsubscribe_operation

__all__ = [
    "CreateCommunityInput",
    "CreateCommunityResult",
    "ListSubscriptionsInput",
    "SubscribeLocalCommunityInput",
    "SubscribeInput",
    "UnsubscribeLocalCommunityInput",
    "UnsubscribeInput",
    "create_community_operation",
    "list_subscriptions_operation",
    "subscribe_local_community_operation",
    "subscribe_operation",
    "unsubscribe_local_community_operation",
    "unsubscribe_operation",
]
