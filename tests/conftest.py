from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@dataclass
class RegisteredCommand:
    # Tests capture the decorated callback so they can execute the real command
    # adapter without needing a live Discord client connection.
    name: str
    description: str
    callback: object


class RecordingTree:
    # The command modules only need the decorator contract from CommandTree.
    # This lightweight test double stores registered callbacks by command name.
    def __init__(self) -> None:
        self.commands: dict[str, RegisteredCommand] = {}

    def command(self, *, name: str, description: str):
        def decorator(callback):
            self.commands[name] = RegisteredCommand(
                name=name,
                description=description,
                callback=callback,
            )
            return callback

        return decorator


@pytest.fixture
def command_tree() -> RecordingTree:
    return RecordingTree()


@pytest.fixture
def interaction() -> AsyncMock:
    # The response mock is the main contract surface these command tests verify.
    # user.id is set to a stable string so commands that record the initiator
    # (e.g. subscribe-channel) get a deterministic value rather than an AsyncMock.
    mock_interaction = AsyncMock()
    mock_interaction.response.send_message = AsyncMock()
    mock_interaction.user.id = "1234567890"
    mock_interaction.guild_id = 99999
    return mock_interaction


@pytest.fixture
def forum_channel() -> SimpleNamespace:
    # Slash commands only rely on the channel identity and Discord mention.
    return SimpleNamespace(id=12345, mention="<#12345>")


@pytest.fixture
def database() -> Mock:
    return Mock()


@pytest.fixture
def lemmy() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def fedify_gateway() -> AsyncMock:
    return AsyncMock()
