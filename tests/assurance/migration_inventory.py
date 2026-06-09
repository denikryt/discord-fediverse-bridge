"""Classify executable tests for the formalized migration completion report."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Literal

Classification = Literal["A", "B", "C", "D", "E"]


@dataclass(frozen=True, slots=True)
class MigrationRecord:
    """Describe one executable test and its deliberate migration status."""

    test_id: str
    runtime: Literal["python", "gateway"]
    domain: str
    classification: Classification
    migration_status: str
    report_participation: bool
    contract_id: str
    intentional_reason: str | None

    def to_json(self) -> dict[str, object]:
        """Return a JSON-ready representation with stable keys."""

        return asdict(self)


_TYPED_FILES = {
    "tests/operations/test_ban_contract_cases.py": "ban",
    "tests/operations/test_bridge_policy_contract_cases.py": "bridge_policy",
    "tests/operations/test_community_management_contract_cases.py": "community_management",
    "tests/operations/test_subscription_contract_cases.py": "subscription",
    "tests/test_identity_discovery_contract_cases.py": "identity_discovery",
}


def _path(test_id: str) -> str:
    """Return the file portion of a pytest node ID or gateway path."""

    return test_id.split("::", 1)[0]


def infer_domain(test_id: str) -> str:
    """Infer the established assurance domain from stable file ownership."""

    value = test_id.lower()
    ordered = (
        ("ban", ("ban_", "user_ban", "banned")),
        (
            "bridge_policy",
            (
                "bridge_policy",
                "federation_policy",
                "command_access_policy",
                "local_community_permissions",
            ),
        ),
        (
            "community_management",
            (
                "community_management",
                "community_disabled",
                "disabled_scenarios",
                "edit_metadata",
                "local_community_registration",
            ),
        ),
        (
            "identity_discovery",
            (
                "identity",
                "discovery",
                "community_labels",
                "lemmyverse",
                "directory_snapshot",
                "registration_scenarios",
            ),
        ),
        (
            "subscription",
            ("subscription", "subscribe", "unsubscribe", "follow", "unfollow"),
        ),
        (
            "outbound_fanout",
            (
                "fanout",
                "routing_metadata",
                "phase2_",
                "phase3_",
                "local_subscriber",
                "remote_fanout",
            ),
        ),
        (
            "inbound_activitypub",
            (
                "inbound",
                "activitypub",
                "internal_fedify",
                "backfill",
                "phase5_",
                "phase6_",
            ),
        ),
        (
            "content_lifecycle",
            (
                "publish",
                "reply",
                "edit_delete",
                "edit-delete",
                "phase4_",
                "phase8_",
                "phase9_",
                "dedup_flow",
                "mirror_messages",
            ),
        ),
        (
            "technical_contracts",
            (
                "dashboard",
                "oauth",
                "public_base",
                "docker",
                "backup",
                "schema_cleanup",
                "verify-",
                "gateway",
            ),
        ),
    )
    for domain, markers in ordered:
        if any(marker in value for marker in markers):
            return domain
    if value.startswith("vendor/discordops/"):
        return "discordops_framework"
    return "core_or_support"


def _contract_id(domain: str, test_id: str) -> str:
    """Create a stable machine-readable ID from the executable node identity."""

    slug = re.sub(r"[^a-z0-9]+", ".", test_id.lower()).strip(".")
    return f"{domain}:{slug}"


def classify_test(
    test_id: str, runtime: Literal["python", "gateway"]
) -> MigrationRecord:
    """Classify one executable test without inspecting production behavior."""

    path = _path(test_id)
    domain = infer_domain(test_id)
    if runtime == "gateway":
        classification: Classification = "D"
        status = "intentional_native_technical_contract"
        reason = "Gateway verification remains native TypeScript technical coverage."
    elif path in _TYPED_FILES:
        classification = "A"
        domain = _TYPED_FILES[path]
        status = "formalized_typed_contract"
        reason = None
    elif path.startswith(
        ("tests/property/", "tests/stateful/", "tests/assurance/")
    ) or path.endswith("test_ban_pairwise_interactions.py"):
        classification = "C"
        status = "formalized_generated_assurance"
        reason = None
    elif path.startswith("tests/behavior/") or any(
        token in path
        for token in ("test_phase", "test_end_to_end", "test_discord_publish_flow")
    ):
        classification = "B"
        status = "intentional_named_scenario"
        reason = (
            "Narrative setup, ordering, or transport-specific effects remain "
            "clearer as an explicit scenario."
        )
    else:
        classification = "D"
        status = "intentional_native_contract"
        reason = (
            "Focused unit, integration, framework, or infrastructure contract "
            "does not benefit from typed domain cases."
        )
    return MigrationRecord(
        test_id=test_id,
        runtime=runtime,
        domain=domain,
        classification=classification,
        migration_status=status,
        report_participation=True,
        contract_id=_contract_id(domain, test_id),
        intentional_reason=reason,
    )
