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
            "bot_token": "bot-secret-not-to-log",
            "transport_secret": "rpc-secret-not-to-log",
            "rpc_url": "http://127.0.0.1:8769",
        }
        instance.update(overrides)
        return instance

    @staticmethod
    def _settings(raw: str | None) -> Settings:
        """Construct Settings through its real environment-keyed validation."""
        return Settings.model_validate({
            "DISCORD_TOKEN": "discord-test",
            "PUBLIC_BASE_URL": "https://bridge.example",
            "FEDIFY_SHARED_SECRET": "gateway-secret",
            "BRIDGE_SUPER_ADMIN_USER_IDS": "1",
            **({"FLUXER_CONNECTORS_JSON": raw} if raw is not None else {}),
        })

    def test_disconnected_default_has_no_connector_config(self) -> None:
        """No env opt-in means no Fluxer client, socket, or DB provisioning."""
        settings = self._settings(None)
        self.assertEqual(settings.fluxer_connectors, ())
        self.assertEqual(parse_fluxer_connectors(None), ())

    def test_explicit_multiple_instances_are_canonical_and_secrets_redacted(self) -> None:
        """Validated connector settings retain identity without leaking tokens."""
        data = [self._instance(), self._instance(
            connector_key="fluxer-east", origin="https://elsewhere.example",
            rpc_url="http://127.0.0.1:8770",
        )]
        settings = self._settings(json.dumps(data))
        self.assertEqual(len(settings.fluxer_connectors), 2)
        self.assertEqual(settings.fluxer_connectors[0].origin, "https://fluxer.example")
        for string in (repr(settings), repr(settings.fluxer_connectors)):
            self.assertNotIn("bot-secret-not-to-log", string)
            self.assertNotIn("rpc-secret-not-to-log", string)

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
            self._instance(bot_token=""),
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
