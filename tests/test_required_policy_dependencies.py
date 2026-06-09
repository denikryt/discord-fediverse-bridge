"""Contract tests for mandatory bridge-policy dependency wiring."""

from __future__ import annotations

from inspect import Parameter, signature

import pytest

from src.commands import (
    ban_user,
    create_community,
    edit_community,
    list_banned_users,
    list_subs,
    publish_guild_invite,
    register as register_command,
    remove_guild_invite,
    subscribe,
    unban_user,
    unsubscribe,
)
from src.community_sync.discord_fanout import DiscordFanout
from src.content_publish_service import ContentPublishService
from src.discord_event_router import DiscordEventRouter
from src.local_communities.runtime import LocalCommunityRuntime
from src.operations import (
    BanUserInput,
    EditCommunityInput,
    ListBannedUsersInput,
    SubscribeInput,
    UnbanUserInput,
    UnsubscribeInput,
)
from src.operations.common_preconditions import CommandAccessInput


@pytest.mark.parametrize(
    ("callable_obj", "parameter_name"),
    [
        (DiscordEventRouter, "bridge_policy_service"),
        (ContentPublishService, "bridge_policy_service"),
        (LocalCommunityRuntime, "bridge_policy_service"),
        (DiscordFanout, "policy_service"),
        (CommandAccessInput, "policy_service"),
        (BanUserInput, "policy_service"),
        (EditCommunityInput, "policy_service"),
        (ListBannedUsersInput, "policy_service"),
        (SubscribeInput, "policy_service"),
        (UnbanUserInput, "policy_service"),
        (UnsubscribeInput, "policy_service"),
    ],
)
def test_policy_sensitive_runtime_and_operation_dependencies_are_required(
    callable_obj: object,
    parameter_name: str,
) -> None:
    """Runtime and operation APIs must reject omitted policy dependencies."""
    parameter = signature(callable_obj).parameters[parameter_name]

    assert parameter.default is Parameter.empty


@pytest.mark.parametrize(
    "register_function",
    [
        ban_user.register,
        create_community.register,
        edit_community.register,
        list_banned_users.register,
        list_subs.register,
        publish_guild_invite.register,
        register_command.register,
        remove_guild_invite.register,
        subscribe.register,
        unban_user.register,
        unsubscribe.register,
    ],
)
def test_policy_sensitive_command_registration_requires_shared_service(
    register_function: object,
) -> None:
    """Command factories must receive the composition-root policy service."""
    parameter = signature(register_function).parameters["policy_service"]

    assert parameter.default is Parameter.empty
