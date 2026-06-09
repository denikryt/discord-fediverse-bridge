"""Stable rule ownership for dashboard, configuration, deployment, and gateway tests."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

OwnerKind = Literal["pytest", "gateway"]


@dataclass(frozen=True, slots=True)
class TechnicalContractEntry:
    """Describe one technical rule and its native executable owners."""

    rule_id: str
    family: str
    owner_kind: OwnerKind
    owners: tuple[str, ...]


TECHNICAL_CONTRACT_ENTRIES = (
    TechnicalContractEntry(
        "dashboard_payload_and_redaction",
        "dashboard",
        "pytest",
        ("tests/behavior/test_dashboard_scenarios.py::",),
    ),
    TechnicalContractEntry(
        "sqlite_backup_and_restore",
        "backup",
        "pytest",
        ("tests/behavior/test_sqlite_backup_scenarios.py::",),
    ),
    TechnicalContractEntry(
        "discord_oauth_exchange",
        "oauth",
        "pytest",
        ("tests/test_discord_oauth_client.py::",),
    ),
    TechnicalContractEntry(
        "public_base_url_derivation",
        "configuration",
        "pytest",
        ("tests/test_public_base_url_config.py::",),
    ),
    TechnicalContractEntry(
        "docker_deployment_contract",
        "deployment",
        "pytest",
        ("tests/test_docker_deployment.py::",),
    ),
    TechnicalContractEntry(
        "schema_cleanup_contract",
        "schema",
        "pytest",
        ("tests/test_stage5_schema_cleanup.py::",),
    ),
    TechnicalContractEntry(
        "gateway_typescript_check",
        "gateway",
        "gateway",
        ("check",),
    ),
    TechnicalContractEntry(
        "gateway_actor_and_webfinger",
        "gateway",
        "gateway",
        (
            "tests/verify-actor-layer.ts",
            "tests/verify-webfinger.ts",
        ),
    ),
    TechnicalContractEntry(
        "gateway_follow_protocol",
        "gateway",
        "gateway",
        (
            "tests/verify-accept-follow.ts",
            "tests/verify-follow-lifecycle.ts",
        ),
    ),
    TechnicalContractEntry(
        "gateway_local_community_publish",
        "gateway",
        "gateway",
        (
            "tests/verify-local-community-publish.ts",
            "tests/verify-local-community-canonical-key.ts",
        ),
    ),
    TechnicalContractEntry(
        "gateway_local_community_relay",
        "gateway",
        "gateway",
        ("tests/verify-local-community-relay.ts",),
    ),
    TechnicalContractEntry(
        "gateway_inbox_and_delivery",
        "gateway",
        "gateway",
        (
            "tests/verify-python-contract.ts",
            "tests/verify-direct-update-delete-delivery.ts",
        ),
    ),
    TechnicalContractEntry(
        "gateway_signature_and_keys",
        "gateway",
        "gateway",
        (
            "tests/verify-bridge-actor-key-store.ts",
            "tests/verify-user-canonical-actor.ts",
        ),
    ),
    TechnicalContractEntry(
        "gateway_configuration_contract",
        "gateway",
        "gateway",
        (
            "tests/verify-env-loading.ts",
            "tests/verify-gateway-url.ts",
            "tests/verify-project-version.ts",
        ),
    ),
    TechnicalContractEntry(
        "gateway_remaining_verification",
        "gateway",
        "gateway",
        ("tests/verify-",),
    ),
)
