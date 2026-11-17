"""Opt-in Fluxer configuration regression coverage at Settings admission."""

from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from src.config import Settings
from src.fluxer_connector_config import parse_fluxer_connectors


class FluxerConfigTests(unittest.TestCase):
    """Ensure disabled and invalid configuration never authorizes a sidecar."""

    @staticmethod
    def _instance(**overrides: object) -> dict:
        """Return minimal operator-supplied data for one test connector."""
        instance = {
            "connector_key": "fluxer-west",
            "origin": "https://FLUXER.example:443",
            "transport_secret": "rpc-secret-not-to-log",
            "rpc_url": "http://127.0.0.1:8769",
        }
        instance.update(overrides)
        return instance

    @staticmethod
    def _settings(raw: str | None) -> Settings:
        """Construct Settings through its real environment-keyed validation."""
        # The operator chooses one platform per service, not an opt-in dual bot.
        selected = raw is not None
        return Settings.model_validate({
            "BRIDGE_PLATFORM": "fluxer" if selected else "discord",
            "PUBLIC_BASE_URL": "https://bridge.example",
            "FEDIFY_SHARED_SECRET": "gateway-secret",
            **({"DISCORD_TOKEN": "discord-test", "BRIDGE_SUPER_ADMIN_USER_IDS": "1"}
               if not selected else {}),
            **({"FLUXER_CONNECTORS_JSON": raw} if selected else {}),
        })

    def test_platform_env_absent_vs_empty_in_environment_and_dotenv(self) -> None:
        """Empty explicit selector refuses startup even above a valid .env value."""
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        base = {'DISCORD_TOKEN': 'discord-test', 'BRIDGE_SUPER_ADMIN_USER_IDS': '123',
                'PUBLIC_BASE_URL': 'https://bridge.example',
                'FEDIFY_SHARED_SECRET': 'gateway-secret'}
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / '.env'
            path.write_text('BRIDGE_PLATFORM=discord\n')
            for value in ('', '   ', 'invalid'):
                with patch.dict(os.environ, {'BRIDGE_PLATFORM': value}, clear=True):
                    with self.subTest(value=value), self.assertRaises(ValidationError):
                        Settings(_env_file=path, **base)
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(Settings(_env_file=path, **base).bridge_platform, 'discord')
                path.write_text('BRIDGE_PLATFORM=\n')
                with self.assertRaises(ValidationError):
                    Settings(_env_file=path, **base)
                path.write_text('BRIDGE_PLATFORM=fluxer\n')
                with self.assertRaises(ValidationError):
                    Settings(_env_file=path, **base)
                self.assertEqual(Settings(_env_file=None, **base).bridge_platform, 'discord')

    def test_disconnected_default_has_no_connector_config(self) -> None:
        """No env opt-in means no Fluxer client, socket, or DB provisioning."""
        settings = self._settings(None)
        self.assertEqual(settings.fluxer_connectors, ())
        self.assertEqual(parse_fluxer_connectors(None), ())

    def test_one_trusted_instance_is_canonical_and_bearer_redacted(self) -> None:
        """One token-free config is accepted and cannot serialize a bot credential."""
        settings = self._settings(json.dumps([self._instance()]))
        self.assertEqual(len(settings.fluxer_connectors), 1)
        self.assertEqual(settings.fluxer_connectors[0].origin, "https://fluxer.example")
        self.assertFalse(hasattr(settings.fluxer_connectors[0], "bot_token"))
        for string in (repr(settings), repr(settings.fluxer_connectors)):
            self.assertNotIn("rpc-secret-not-to-log", string)

    def test_second_distinct_instance_refused_before_provisioning(self) -> None:
        """Separate origins are forbidden, even when no SQLite index would collide."""
        instances = [self._instance(), self._instance(
            connector_key="fluxer-east", origin="https://elsewhere.example",
            rpc_url="http://127.0.0.1:8770",
        )]
        with self.assertRaises(ValidationError):
            self._settings(json.dumps(instances))

    def test_legacy_bot_token_rejected_without_secret_in_error(self) -> None:
        """A legacy Python token field is never silently accepted or logged."""
        old = self._instance(bot_token="bot-secret-not-to-log")
        with self.assertRaises(ValidationError) as failure:
            self._settings(json.dumps([old]))
        self.assertNotIn("bot-secret-not-to-log", str(failure.exception))

    def test_duplicate_origin_and_key_fail_startup_before_provisioning(self) -> None:
        """Conflicting input must never be silently truncated or rebound."""
        for second in (
            self._instance(connector_key="another", origin="https://fluxer.example."),
            self._instance(connector_key="fluxer-west", origin="https://other.example"),
        ):
            with self.subTest(second=second["connector_key"]), self.assertRaises(ValidationError):
                self._settings(json.dumps([self._instance(), second]))

    def test_reject_missing_secrets_unknown_fields_and_invalid_origin(self) -> None:
        """Fail closed on incomplete, injected or malformed connector options."""
        for instance in (
            self._instance(transport_secret=""),
            self._instance(extra="unrecognized"),
            self._instance(origin="http://public.example"),
            self._instance(origin="http://localhost"),
            self._instance(origin="https://fluxer.example/path"),
            self._instance(rpc_url="https://user:password@rpc.example"),
            self._instance(rpc_url="https://public-rpc.example:9000"),
        ):
            with self.subTest(instance=instance["origin"]), self.assertRaises(ValidationError):
                self._settings(json.dumps([instance]))

    def test_forum_tag_selection_is_immutable_and_bounded(self) -> None:
        """Only exact forum ID → unique tag IDs reach the configured adapter."""
        cfg = self._settings(json.dumps([self._instance(
            forum_applied_tags={'forum-1': ['tag-a', 'tag-b']},
        )])).fluxer_connectors[0]
        self.assertEqual(cfg.forum_applied_tags, (('forum-1', ('tag-a', 'tag-b')),))
        for tags in ({'forum-1': ['tag-a', 'tag-a']},
                     {'forum-1': ['a', 'b', 'c', 'd', 'e', 'f']},
                     {'forum/1': ['tag-a']},
                     {'forum-1': ['bad/tag']},
                     {'forum-1': 'tag-a'}):
            with self.subTest(tags=tags), self.assertRaises(ValidationError):
                self._settings(json.dumps([self._instance(forum_applied_tags=tags)]))

    def test_reserved_connector_and_bearer_strength_fail_before_startup(self) -> None:
        """A reserved Discord key or short private bearer cannot reach SQLite/SDK."""
        for secret in ('1234567', 'x'):
            with self.subTest(secret_length=len(secret)), self.assertRaises(ValidationError):
                self._settings(json.dumps([self._instance(transport_secret=secret)]))
        self.assertEqual(len(self._settings(json.dumps([
            self._instance(transport_secret='12345678')
        ])).fluxer_connectors), 1)
        with self.assertRaises(ValidationError):
            self._settings(json.dumps([self._instance(connector_key='discord-primary')]))

    def test_trailing_dns_dot_refused_before_storage(self) -> None:
        """A dotted DNS origin cannot diverge from the Node authenticated identity."""
        with self.assertRaises(ValidationError):
            self._settings(json.dumps([self._instance(origin='https://FLUXER.example.')]))
        self.assertEqual(self._settings(json.dumps([self._instance(
            origin='https://FLUXER.example:443',
        )])).fluxer_connectors[0].origin, 'https://fluxer.example')

    def test_unicode_origin_requires_ascii_alabel_and_matches_node(self) -> None:
        """A Unicode IDN must not persist Python IDNA-2003 instead of Node WHATWG identity."""
        for raw in ('https://faß.test', 'https://bücher.test'):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                self._settings(json.dumps([self._instance(origin=raw)]))
        self.assertEqual(self._settings(json.dumps([self._instance(
            origin='https://XN--FA-HIA.test:443',
        )])).fluxer_connectors[0].origin, 'https://xn--fa-hia.test')

    def test_bearer_must_be_http_header_safe_ascii(self) -> None:
        """Reject non-ASCII and control characters before RPC or SQLite startup."""
        for value in ('1234567', '1234567é', 'секретсекрет', 'abc\rdefgh',
                      'abc\ndefgh', 'abc\tdefgh', 'abc defgh', 'abcdefgh\x7f'):
            with self.subTest(secret=repr(value)), self.assertRaises(ValidationError):
                self._settings(json.dumps([self._instance(transport_secret=value)]))
        for value in ('12345678', 'aB1-._~+/='):
            with self.subTest(secret=value):
                self.assertEqual(self._settings(json.dumps([
                    self._instance(transport_secret=value),
                ])).fluxer_connectors[0].transport_secret, value)

    def test_private_rpc_cannot_cross_network_namespace(self) -> None:
        """A sidecar DNS name or alternate loopback spelling cannot receive bearer."""
        for rpc_url in (
            "http://fluxer-sidecar:8769", "http://localhost:8769",
            "http://[::1]:8769", "https://127.0.0.1:8769",
            "http://127.0.0.2:8769",
        ):
            with self.subTest(rpc_url=rpc_url), self.assertRaises(ValidationError):
                self._settings(json.dumps([self._instance(rpc_url=rpc_url)]))

    def test_development_http_is_opt_in_and_loopback_only(self) -> None:
        """No accidental plaintext Fluxer API exposure outside local tests."""
        settings = self._settings(json.dumps([self._instance(
            origin="http://127.0.0.1:8443", allow_insecure_local_development=True,
        )]))
        self.assertEqual(settings.fluxer_connectors[0].origin, "http://127.0.0.1:8443")
        with self.assertRaises(ValidationError):
            self._settings(json.dumps([self._instance(
                origin="http://192.168.1.1", allow_insecure_local_development=True,
            )]))


if __name__ == "__main__":
    unittest.main()
