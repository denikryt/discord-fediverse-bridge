"""The production Discord router exposes one typed Community Create pipeline.

Check the actual source interface without importing the SDK in a constrained
build environment. Typed runtime behavior is exercised by SQLite scenarios.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path


class SingleCommunityCreateRouteContract(unittest.TestCase):
    """Prevent reintroducing an SDK-shaped entry point around canonical admission."""

    def test_router_contains_no_raw_community_create_api(self) -> None:
        """Only verified normalized Create handlers may route Community publication."""
        source = Path(__file__).resolve().parents[1] / 'src/discord_event_router.py'
        cls = next(n for n in ast.parse(source.read_text()).body
                   if isinstance(n, ast.ClassDef) and n.name == 'DiscordEventRouter')
        methods = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertIn('handle_platform_thread_create', methods)
        self.assertIn('handle_platform_message_create', methods)
        self.assertNotIn('handle_thread_create', methods)
        self.assertNotIn('handle_message', methods)

    def test_router_does_not_delegate_to_raw_community_runtime(self) -> None:
        """Future edits cannot silently reinstate an old SDK create dispatch."""
        source = Path(__file__).resolve().parents[1] / 'src/discord_event_router.py'
        attributes = [n.attr for n in ast.walk(ast.parse(source.read_text()))
                      if isinstance(n, ast.Attribute)]
        self.assertNotIn('handle_discord_thread_create', attributes)
        self.assertNotIn('handle_discord_message', attributes)

    def test_domain_runtimes_have_only_typed_create_entry_points(self) -> None:
        """Legacy SDK handlers cannot reintroduce independent canonical writes."""
        root = Path(__file__).resolve().parents[1]
        for filename, class_name in (
            ('src/community_sync/runtime.py', 'CommunityRuntime'),
            ('src/local_communities/runtime.py', 'LocalCommunityRuntime'),
        ):
            with self.subTest(runtime=class_name):
                cls = next(n for n in ast.parse((root / filename).read_text()).body
                           if isinstance(n, ast.ClassDef) and n.name == class_name)
                methods = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
                self.assertNotIn('handle_discord_thread_create', methods)
                self.assertNotIn('handle_discord_message', methods)
                self.assertIn('handle_normalized_thread_create', methods)
                self.assertIn('handle_normalized_message_create', methods)


if __name__ == '__main__':
    unittest.main()
