"""Behavior tests for the account-settings Discord adapter boundaries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.commands.account_settings import (
    GuildTextChannelTransformer,
    ProfileMetadataModal,
    _parse_metadata_row,
    _run_owned_sync_mutation,
)
from src.user_profiles import UserProfileValidationError


def test_metadata_row_parser_supports_multiline_values_and_rejects_partial_rows() -> (
    None
):
    """Each modal position uses first-line name and remaining-line value semantics."""
    assert _parse_metadata_row("") is None
    assert _parse_metadata_row("About\nFirst line\nSecond line") == (
        "About",
        "First line\nSecond line",
    )
    with pytest.raises(UserProfileValidationError):
        _parse_metadata_row("Name only")
    with pytest.raises(UserProfileValidationError):
        _parse_metadata_row("\nValue only")


def test_metadata_modal_has_four_independent_prefilled_inputs() -> None:
    """Persisted metadata positions map one-to-one onto four Discord inputs."""
    modal = ProfileMetadataModal(
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        rows=[("About", "First\nSecond"), ("Site", "https://example.com")],
    )
    assert len(modal.row_inputs) == 4
    assert [item.default for item in modal.row_inputs] == [
        "About\nFirst\nSecond",
        "Site\nhttps://example.com",
        "",
        "",
    ]


@pytest.mark.asyncio
async def test_started_media_mutation_is_owned_through_callback_cancellation() -> None:
    """Cancellation waits for the worker and observes its completion."""
    started = asyncio.Event()
    release = asyncio.Event()
    completed: list[str] = []

    def operation() -> str:
        loop = asyncio.new_event_loop()
        started_loop = started
        # Notify the test thread-safely, then block until release is signalled.
        asyncio.run_coroutine_threadsafe(_set_event(started_loop), main_loop).result()
        asyncio.run_coroutine_threadsafe(release.wait(), main_loop).result()
        completed.append("done")
        loop.close()
        return "ok"

    async def invoke() -> str:
        return await _run_owned_sync_mutation(operation)

    main_loop = asyncio.get_running_loop()
    task = asyncio.create_task(invoke())
    await started.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed == ["done"]


async def _set_event(event: asyncio.Event) -> None:
    """Set an asyncio event from the worker thread via the owning loop."""
    event.set()


@pytest.mark.asyncio
async def test_media_modal_defers_before_download_and_worker_submission(
    monkeypatch,
) -> None:
    """A media submission acknowledges Discord before any slow operation starts."""
    from src.commands import account_settings

    events: list[str] = []

    class Attachment:
        size = 10

        async def read(self) -> bytes:
            events.append("read")
            return b"image"

    class Response:
        async def defer(self, **_kwargs) -> None:
            events.append("defer")

    class Interaction:
        user = SimpleNamespace(id=7)
        response = Response()

        async def edit_original_response(self, **_kwargs) -> None:
            events.append("edit")

    async def resolve(*_args, **_kwargs):
        return SimpleNamespace(id=11)

    async def run(operation):
        events.append("worker")
        operation()

    async def fanout(_result):
        return SimpleNamespace(render_pending=False, outstanding=0)

    service = SimpleNamespace(
        replace_media=lambda *_args: (
            events.append("mutation") or SimpleNamespace(source_id=1)
        ),
        fanout=fanout,
    )
    settings = SimpleNamespace(profile_media_max_upload_bytes=100)
    modal = account_settings.ProfileMediaModal(
        SimpleNamespace(), settings, SimpleNamespace(), service, "avatar"
    )
    modal.upload._values = [Attachment()]
    monkeypatch.setattr(account_settings, "_resolve_authorized_user", resolve)
    monkeypatch.setattr(account_settings, "_run_owned_sync_mutation", run)

    await modal.on_submit(Interaction())

    assert events == ["defer", "read", "worker", "mutation", "edit"]


@pytest.mark.asyncio
async def test_metadata_modal_replaces_ordered_rows_atomically_with_multiline_values(
    tmp_path, monkeypatch
) -> None:
    """A real repository transaction stores four-position input as ordered fields."""
    from unittest.mock import AsyncMock

    from src.commands import account_settings
    from src.db import Database
    from src.profile_media import ProfileMediaStore
    from src.user_profiles import UserProfileService

    database = Database(f"sqlite:///{tmp_path / 'profile.db'}")
    database.create_all()
    database.migrate()
    user = database.users.create_user(
        discord_user_id="7",
        activitypub_username="alice",
        actor_url="https://bridge.example/users/alice",
        inbox_url="https://bridge.example/users/alice/inbox",
        outbox_url="https://bridge.example/users/alice/outbox",
        followers_url="https://bridge.example/users/alice/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    store = ProfileMediaStore(
        tmp_path / "media", max_upload_bytes=1024, max_pixels=1000
    )
    store.initialize()
    service = UserProfileService(database, store)
    service.update_identity_text(user.id, "Alice", "Existing bio")
    service.replace_metadata(user.id, [("Old", "Value")])
    modal = ProfileMetadataModal(
        database, SimpleNamespace(), SimpleNamespace(), service
    )
    values = [
        "About\nFirst line\nSecond line",
        "",
        "Site\nhttps://example.com",
        "Pronouns\nthey/them",
    ]
    for item, value in zip(modal.row_inputs, values, strict=True):
        item._value = value
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=7),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    async def authorized(*_args, **_kwargs):
        return user

    monkeypatch.setattr(account_settings, "_resolve_authorized_user", authorized)
    await modal.on_submit(interaction)

    profile = service.get_profile(user.id)
    assert profile is not None
    assert profile.display_name == "Alice"
    assert profile.summary == "Existing bio"
    assert profile.fields == (
        ("About", "First line\nSecond line"),
        ("Site", "https://example.com"),
        ("Pronouns", "they/them"),
    )


@pytest.mark.asyncio
async def test_metadata_modal_partial_row_rejects_whole_replacement(
    tmp_path, monkeypatch
) -> None:
    """One partial position prevents every metadata change in the submission."""
    from unittest.mock import AsyncMock

    from src.commands import account_settings
    from src.db import Database
    from src.profile_media import ProfileMediaStore
    from src.user_profiles import UserProfileService

    database = Database(f"sqlite:///{tmp_path / 'profile.db'}")
    database.create_all()
    database.migrate()
    user = database.users.create_user(
        discord_user_id="7",
        activitypub_username="alice",
        actor_url="https://bridge.example/users/alice",
        inbox_url="https://bridge.example/users/alice/inbox",
        outbox_url="https://bridge.example/users/alice/outbox",
        followers_url="https://bridge.example/users/alice/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    store = ProfileMediaStore(
        tmp_path / "media", max_upload_bytes=1024, max_pixels=1000
    )
    store.initialize()
    service = UserProfileService(database, store)
    service.replace_metadata(user.id, [("Existing", "Preserved")])
    modal = ProfileMetadataModal(
        database, SimpleNamespace(), SimpleNamespace(), service
    )
    modal.row_inputs[0]._value = "New\nValue"
    modal.row_inputs[1]._value = "Partial only"
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=7),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    async def authorized(*_args, **_kwargs):
        return user

    monkeypatch.setattr(account_settings, "_resolve_authorized_user", authorized)
    await modal.on_submit(interaction)

    assert service.get_profile(user.id).fields == (("Existing", "Preserved"),)
    interaction.edit_original_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_profile_modal_policy_change_prevents_mutation(monkeypatch) -> None:
    """A user who loses access after opening the modal cannot mutate profile state."""
    from unittest.mock import AsyncMock, Mock

    from src.commands import account_settings

    service = Mock()
    modal = account_settings.ProfileTextModal(
        SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), service
    )
    modal.display_name._value = "Changed"
    modal.summary._value = "Changed bio"
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=7),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    async def denied(*_args, **_kwargs):
        return None

    monkeypatch.setattr(account_settings, "_resolve_authorized_user", denied)
    await modal.on_submit(interaction)

    service.update_identity_text.assert_not_called()


@pytest.mark.asyncio
async def test_media_modal_rejects_oversize_before_defer_or_download(
    monkeypatch,
) -> None:
    """Declared-size preflight rejects an upload without network or durable work."""
    from unittest.mock import AsyncMock, Mock

    from src.commands import account_settings

    attachment = SimpleNamespace(size=101, read=AsyncMock())
    response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock())
    interaction = SimpleNamespace(user=SimpleNamespace(id=7), response=response)
    service = Mock()
    modal = account_settings.ProfileMediaModal(
        SimpleNamespace(),
        SimpleNamespace(profile_media_max_upload_bytes=100),
        SimpleNamespace(),
        service,
        "avatar",
    )
    modal.upload._values = [attachment]

    async def authorized(*_args, **_kwargs):
        return SimpleNamespace(id=1)

    monkeypatch.setattr(account_settings, "_resolve_authorized_user", authorized)
    await modal.on_submit(interaction)

    response.send_message.assert_awaited_once()
    response.defer.assert_not_awaited()
    attachment.read.assert_not_awaited()
    service.replace_media.assert_not_called()


@pytest.mark.asyncio
async def test_media_modal_download_failure_preserves_slot_and_returns_bounded_error(
    monkeypatch,
) -> None:
    """A failed attachment read cannot enter the synchronous media mutation."""
    from unittest.mock import AsyncMock, Mock

    from src.commands import account_settings

    attachment = SimpleNamespace(
        size=10, read=AsyncMock(side_effect=OSError("cdn secret"))
    )
    response = SimpleNamespace(defer=AsyncMock())
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=7),
        response=response,
        edit_original_response=AsyncMock(),
    )
    service = Mock()
    modal = account_settings.ProfileMediaModal(
        SimpleNamespace(),
        SimpleNamespace(profile_media_max_upload_bytes=100),
        SimpleNamespace(),
        service,
        "header",
    )
    modal.upload._values = [attachment]

    async def authorized(*_args, **_kwargs):
        return SimpleNamespace(id=1)

    monkeypatch.setattr(account_settings, "_resolve_authorized_user", authorized)
    await modal.on_submit(interaction)

    response.defer.assert_awaited_once()
    attachment.read.assert_awaited_once()
    service.replace_media.assert_not_called()
    interaction.edit_original_response.assert_awaited_once_with(
        content="The image could not be processed."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        (
            SimpleNamespace(render_pending=True, outstanding=1),
            "Profile updated locally, but federation refresh is pending.",
        ),
        (
            SimpleNamespace(render_pending=False, outstanding=1),
            "Profile updated locally. Some remote followers may refresh later.",
        ),
        (
            SimpleNamespace(render_pending=False, outstanding=0),
            "Profile updated.",
        ),
    ],
)
async def test_profile_mutation_response_separates_local_success_from_remote_state(
    summary, expected
) -> None:
    """Command output never asks the user to repeat an already committed mutation."""
    from unittest.mock import AsyncMock

    from src.commands.account_settings import _finish_profile_mutation

    service = SimpleNamespace(fanout=AsyncMock(return_value=summary))
    interaction = SimpleNamespace(edit_original_response=AsyncMock())
    result = SimpleNamespace(source_id=17)

    await _finish_profile_mutation(
        interaction, service, result, success_message="Profile updated."
    )

    interaction.edit_original_response.assert_awaited_once_with(content=expected)


def _registered_account_settings_group(database, personal_feed_service):
    """Register the real command callbacks and return the captured command group."""
    from src.commands.account_settings import register

    class Tree:
        command = None

        def add_command(self, command):
            self.command = command

    tree = Tree()
    register(
        tree,
        database,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        personal_feed_service,
    )
    return tree.command


@pytest.mark.asyncio
async def test_feed_view_callback_reports_unbound_and_bound_state(
    tmp_path, monkeypatch
) -> None:
    """The real feed-view callback reads an existing two-entity binding row."""
    from unittest.mock import AsyncMock

    from src.commands import account_settings
    from src.db import Database
    from src.models import User
    from src.personal_feeds import PersonalFeedService

    database = Database(f"sqlite:///{tmp_path / 'bridge.sqlite3'}")
    database.create_all()
    database.migrate()
    with database.session() as session:
        session.add(
            User(
                discord_user_id="7",
                activitypub_username="alice",
                actor_url="https://bridge.test/users/alice",
                inbox_url="https://bridge.test/users/alice/inbox",
                outbox_url="https://bridge.test/users/alice/outbox",
                followers_url="https://bridge.test/users/alice/followers",
                public_key_pem="public",
                private_key_pem="private",
            )
        )
    user = database.users.get_user_by_discord_user_id("7")
    service = PersonalFeedService(database)
    group = _registered_account_settings_group(database, service)
    command = group.get_command("feed-view")
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=7),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    async def authorized(*_args, **_kwargs):
        return user

    monkeypatch.setattr(account_settings, "_resolve_authorized_user", authorized)

    await command.callback(interaction)
    interaction.response.send_message.assert_awaited_once_with(
        "No personal-feed channel is bound.", ephemeral=True
    )

    interaction.response.send_message.reset_mock()
    assert service.bind_or_move("7", 100, 200).status == "bound"
    await command.callback(interaction)
    interaction.response.send_message.assert_awaited_once_with(
        "Personal feed: <#200> (guild `100`).", ephemeral=True
    )
    database.close()


def test_feed_bind_registration_allows_text_and_announcement_channels() -> None:
    """The command picker advertises every supported personal-feed root channel type."""
    import discord

    group = _registered_account_settings_group(SimpleNamespace(), SimpleNamespace())
    command = group.get_command("feed-bind")
    parameter = command.parameters[0]

    assert parameter.name == "channel"
    assert parameter.type is discord.AppCommandOptionType.channel
    assert set(parameter.channel_types) == {
        discord.ChannelType.text,
        discord.ChannelType.news,  # Discord API GUILD_ANNOUNCEMENT
    }


@pytest.mark.asyncio
async def test_feed_bind_transformer_resolves_selected_announcement_channel() -> None:
    """The live Discord picker path resolves selected wrappers before validation."""
    import discord

    announcement = object.__new__(discord.TextChannel)
    # Discord API GUILD_ANNOUNCEMENT; discord.py exposes it as news.
    announcement._type = discord.ChannelType.news.value
    selected = SimpleNamespace(resolve=lambda: announcement)

    result = await GuildTextChannelTransformer().transform(SimpleNamespace(), selected)

    assert result is announcement


@pytest.mark.asyncio
async def test_feed_bind_transformer_rejects_unresolved_channel() -> None:
    """Unresolved command-channel wrappers must fail before callback mutation."""
    from discord import app_commands

    selected = SimpleNamespace(resolve=lambda: None)

    with pytest.raises(app_commands.TransformerError):
        await GuildTextChannelTransformer().transform(SimpleNamespace(), selected)


@pytest.mark.asyncio
async def test_feed_bind_accepts_announcement_channel_before_mutation(
    monkeypatch,
) -> None:
    """Runtime validation permits announcement channels and preserves owned mutation flow."""
    import discord
    from unittest.mock import AsyncMock, Mock

    from src.commands import account_settings

    service = Mock()
    service.get_by_discord_user_id.return_value = None
    service.bind_or_move.return_value = SimpleNamespace(status="bound")
    group = _registered_account_settings_group(SimpleNamespace(), service)
    command = group.get_command("feed-bind")
    interaction = SimpleNamespace(
        user=SimpleNamespace(
            id=7,
            guild_permissions=SimpleNamespace(manage_guild=True),
        ),
        guild=SimpleNamespace(id=100),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )
    announcement = object.__new__(discord.TextChannel)
    # Discord API GUILD_ANNOUNCEMENT; discord.py exposes it as news.
    announcement._type = discord.ChannelType.news.value
    announcement.guild = SimpleNamespace(id=100)
    announcement.id = 250

    async def authorized(*_args, **_kwargs):
        return SimpleNamespace(id=11)

    monkeypatch.setattr(account_settings, "_resolve_authorized_user", authorized)

    await command.callback(interaction, announcement)

    interaction.response.send_message.assert_not_awaited()
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    service.bind_or_move.assert_called_once_with("7", 100, 250, expected_binding_id=None)
    interaction.edit_original_response.assert_awaited_once_with(
        content="Personal-feed channel bound."
    )


@pytest.mark.asyncio
async def test_feed_bind_rejects_cross_guild_announcement_before_mutation(
    monkeypatch,
) -> None:
    """Same type support must not weaken same-guild authority checks."""
    import discord
    from unittest.mock import AsyncMock, Mock

    from src.commands import account_settings

    service = Mock()
    group = _registered_account_settings_group(SimpleNamespace(), service)
    command = group.get_command("feed-bind")
    interaction = SimpleNamespace(
        user=SimpleNamespace(
            id=7,
            guild_permissions=SimpleNamespace(manage_guild=True),
        ),
        guild=SimpleNamespace(id=100),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )
    announcement = object.__new__(discord.TextChannel)
    # Discord API GUILD_ANNOUNCEMENT; discord.py exposes it as news.
    announcement._type = discord.ChannelType.news.value
    announcement.guild = SimpleNamespace(id=999)
    announcement.id = 250

    async def authorized(*_args, **_kwargs):
        return SimpleNamespace(id=11)

    monkeypatch.setattr(account_settings, "_resolve_authorized_user", authorized)

    await command.callback(interaction, announcement)

    interaction.response.send_message.assert_awaited_once_with(
        "Choose a text or announcement channel in this server.",
        ephemeral=True,
    )
    interaction.response.defer.assert_not_awaited()
    service.bind_or_move.assert_not_called()
