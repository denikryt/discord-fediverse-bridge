"""Machine-readable rule ownership for outbound fanout scenarios."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Literal


def _node(module: str, test: str) -> str:
    """Build a pytest node ID without storing an unreadable long literal."""

    return f"{module}::{test}"


Classification = Literal["typed_case", "named_scenario", "generated_assurance"]


@dataclass(frozen=True, slots=True)
class FanoutContractEntry:
    rule_id: str
    family: str
    classification: Classification
    node_prefixes: tuple[str, ...]


FANOUT_CONTRACT_ENTRIES = (
    FanoutContractEntry(
        "remote_single_target",
        "target_selection",
        "named_scenario",
        (
            _node(
                "tests/test_phase2_fanout_scenarios.py",
                "test_phase2_single_subscription_no_mirror_created",
            ),
            _node(
                "tests/test_phase3_message_fanout_scenarios.py",
                "test_phase3_single_subscription_message_published_no_mirror",
            ),
        ),
    ),
    FanoutContractEntry(
        "remote_sibling_delivery",
        "target_selection",
        "named_scenario",
        (
            _node(
                "tests/test_phase2_fanout_scenarios.py",
                "test_phase2_two_subscriptions_mirror_thread_created_in_sibling",
            ),
            _node(
                "tests/test_phase3_message_fanout_scenarios.py",
                "test_phase3_two_subscriptions_message_mirrored_to_sibling_thread",
            ),
        ),
    ),
    FanoutContractEntry(
        "duplicate_suppression",
        "dedup_retry",
        "named_scenario",
        (
            _node(
                "tests/test_phase2_fanout_scenarios.py",
                "test_phase2_duplicate_thread_create_is_ignored",
            ),
            _node(
                "tests/test_phase3_message_fanout_scenarios.py",
                "test_phase3_duplicate_message_is_ignored",
            ),
        ),
    ),
    FanoutContractEntry(
        "source_survives_mirror_failure",
        "healthy_target_isolation",
        "named_scenario",
        (
            _node(
                "tests/test_phase2_fanout_scenarios.py",
                "test_phase2_mirror_failure_does_not_block_source_publish",
            ),
            _node(
                "tests/test_phase3_message_fanout_scenarios.py",
                "test_phase3_mirror_message_failure_does_not_block_source_publish",
            ),
        ),
    ),
    FanoutContractEntry(
        "malformed_remote_target_isolated",
        "routing_metadata",
        "typed_case",
        (
            _node(
                "tests/test_policy_routing_metadata.py",
                "test_remote_thread_fanout_isolates_missing_and_malformed_targets",
            ),
            _node(
                "tests/test_policy_routing_metadata.py",
                "test_remote_target_rejects_invalid_guild_ids",
            ),
        ),
    ),
    FanoutContractEntry(
        "policy_failure_fail_closed",
        "routing_metadata",
        "typed_case",
        (
            _node(
                "tests/test_policy_routing_metadata.py",
                "test_remote_policy_failure_is_fail_closed",
            ),
        ),
    ),
    FanoutContractEntry(
        "local_invalid_target_isolated",
        "routing_metadata",
        "typed_case",
        (
            _node(
                "tests/test_policy_routing_metadata.py",
                "test_local_thread_fanout_skips_invalid_target_and_continues_healthy_target",
            ),
            _node(
                "tests/test_policy_routing_metadata.py",
                "test_local_surface_missing_or_malformed_metadata_is_fail_closed",
            ),
        ),
    ),
    FanoutContractEntry(
        "local_subscriber_surface_creation",
        "mapping_receipt",
        "named_scenario",
        (
            _node(
                "tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py",
                "test_host_thread_create_fans_out_to_local_subscriber_thread_surfaces",
            ),
            _node(
                "tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py",
                "test_local_subscriber_thread_create_creates_source_host_and_sibling_surfaces",
            ),
        ),
    ),
    FanoutContractEntry(
        "parent_mapping_per_target",
        "mapping_receipt",
        "named_scenario",
        (
            _node(
                "tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py",
                "test_host_root_and_nested_replies_use_surface_local_parent_ids",
            ),
            _node(
                "tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py",
                "test_local_subscriber_nested_reply_maps_parent_surface_per_target",
            ),
        ),
    ),
    FanoutContractEntry(
        "missing_surface_retry_only",
        "dedup_retry",
        "named_scenario",
        (
            _node(
                "tests/behavior/test_local_community_stage3_local_subscriber_sync_scenarios.py",
                "test_duplicate_source_processing_retries_missing_surfaces_only",
            ),
            _node(
                "tests/behavior/test_local_community_stage4_local_subscriber_origin_scenarios.py",
                "test_duplicate_local_subscriber_thread_retries_missing_targets_without_republish",
            ),
        ),
    ),
    FanoutContractEntry(
        "relay_target_isolation_retry",
        "healthy_target_isolation",
        "generated_assurance",
        (
            _node(
                "tests/behavior/test_local_community_remote_fanout_scenarios.py",
                "test_partial_relay_failure_retries_only_failed_target",
            ),
        ),
    ),
    FanoutContractEntry(
        "relay_policy_snapshot_visibility",
        "policy_snapshot",
        "generated_assurance",
        (
            _node(
                "tests/behavior/test_local_community_remote_fanout_scenarios.py",
                "test_policy_change_during_relay_applies_to_next_action_only",
            ),
        ),
    ),
)
