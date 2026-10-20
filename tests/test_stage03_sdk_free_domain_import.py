"""Prevent accidental Discord SDK import at the SDK-independent Community boundary."""

from __future__ import annotations

import subprocess
import sys


def test_community_runtime_and_content_helpers_import_without_discord():
    """Importing bridge domain types must not require a connected Discord SDK."""
    script = '''
import importlib.abc
import sys
class RejectDiscord(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'discord' or fullname.startswith('discord.'):
            raise RuntimeError('SDK import crossed domain boundary: ' + fullname)
sys.meta_path.insert(0, RejectDiscord())
from src.community_sync.runtime import CommunityRuntime
from src.content_sync import resolve_published_object_for_discord_message
assert CommunityRuntime is not None
assert resolve_published_object_for_discord_message is not None
assert 'discord' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-c', script], text=True,
                            capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_adapter_remains_a_separate_importable_module():
    """The typed adapter can be imported without starting the Discord client."""
    script = 'from src.discord_adapter import DiscordAdapter; assert DiscordAdapter is not None'
    result = subprocess.run([sys.executable, '-c', script], text=True,
                            capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
