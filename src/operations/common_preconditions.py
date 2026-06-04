"""Reusable DiscordOps preconditions shared by commands and operations."""

from __future__ import annotations

from typing import Protocol

from discordops import Precondition

REGISTRATION_REQUIRED_MESSAGE = "You must register with the bridge before using this command. Use `/register` first."
SUBSCRIPTION_REGISTRATION_REQUIRED_MESSAGE = "You must register with the bridge before subscribing a channel. Use `/register` first."


class RegisteredDiscordUserInput(Protocol):
    """Structural contract for inputs that can resolve a registered bridge user."""

    def get_bridge_user(self) -> object | None:
        """Return the registered bridge user or ``None`` when registration is absent."""
        ...


def _discord_user_is_registered(value: RegisteredDiscordUserInput) -> bool:
    """Return whether the input resolves an existing registered bridge user."""
    return value.get_bridge_user() is not None


def _registration_required_message(value: RegisteredDiscordUserInput) -> str:
    """Preserve adapter-specific registration wording through one condition object."""
    # Subscription operation inputs expose channel identity. Command-access
    # inputs intentionally do not, so the shared precondition can retain both
    # established user-visible contracts without duplicating policy logic.
    if hasattr(value, "channel_id"):
        return SUBSCRIPTION_REGISTRATION_REQUIRED_MESSAGE
    return REGISTRATION_REQUIRED_MESSAGE


DISCORD_USER_REGISTERED = Precondition(
    name="discord_user_is_registered",
    message=_registration_required_message,
    predicate=_discord_user_is_registered,
)
