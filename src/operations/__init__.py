"""App-specific operation contracts for Discord command policy."""

from .ban_user import BanUserInput, BanUserResult, ban_user_operation
from .unban_user import UnbanUserInput, UnbanUserResult, unban_user_operation
from .list_banned_users import ListBannedUsersInput, ListBannedUsersResult, list_banned_users_operation
from .create_community import (
    CreateCommunityInput,
    CreateCommunityResult,
    create_community_operation,
)
from .edit_community import EditCommunityInput, EditCommunityResult, edit_community_operation
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
from .publish_guild_invite import (
    PublishGuildInviteInput,
    publish_guild_invite_operation,
    run_publish_guild_invite,
)
from .remove_guild_invite import (
    RemoveGuildInviteInput,
    remove_guild_invite_operation,
    run_remove_guild_invite,
)

__all__ = [
    "BanUserInput",
    "BanUserResult",
    "UnbanUserInput",
    "UnbanUserResult",
    "ListBannedUsersInput",
    "ListBannedUsersResult",
    "CreateCommunityInput",
    "CreateCommunityResult",
    "EditCommunityInput",
    "EditCommunityResult",
    "ListSubscriptionsInput",
    "SubscribeLocalCommunityInput",
    "SubscribeInput",
    "UnsubscribeLocalCommunityInput",
    "UnsubscribeInput",
    "ban_user_operation",
    "unban_user_operation",
    "list_banned_users_operation",
    "create_community_operation",
    "edit_community_operation",
    "list_subscriptions_operation",
    "subscribe_local_community_operation",
    "subscribe_operation",
    "unsubscribe_local_community_operation",
    "unsubscribe_operation",
    "PublishGuildInviteInput",
    "RemoveGuildInviteInput",
    "publish_guild_invite_operation",
    "remove_guild_invite_operation",
    "run_publish_guild_invite",
    "run_remove_guild_invite",
]
