"""Rule ownership for publish, reply, edit, and delete lifecycle tests."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Literal


def _node(module: str, test: str) -> str:
    """Build a pytest node ID without storing an unreadable long literal."""

    return f"{module}::{test}"


Classification = Literal["typed_case", "named_scenario", "generated_assurance"]


@dataclass(frozen=True, slots=True)
class ContentContractEntry:
    rule_id: str
    family: str
    classification: Classification
    node_prefixes: tuple[str, ...]


CONTENT_CONTRACT_ENTRIES = (
    ContentContractEntry(
        "remote_post_publish",
        "create",
        "named_scenario",
        (
            _node(
                "tests/behavior/test_publish_scenarios.py",
                "test_registered_user_with_accepted_subscription_publishes_thread_starter",
            ),
            _node(
                "tests/test_discord_publish_flow.py",
                "test_thread_starter_from_registered_user_publishes_and_persists_mappings",
            ),
        ),
    ),
    ContentContractEntry(
        "remote_comment_publish",
        "create",
        "named_scenario",
        (
            _node(
                "tests/behavior/test_publish_scenarios.py",
                "test_registered_user_with_accepted_subscription_publishes_thread_reply",
            ),
            _node(
                "tests/test_discord_publish_flow.py",
                "test_thread_message_from_registered_user_publishes_as_comment",
            ),
        ),
    ),
    ContentContractEntry(
        "registration_subscription_gates",
        "authorization",
        "typed_case",
        (
            _node(
                "tests/behavior/test_publish_scenarios.py",
                "test_unregistered_user_message_is_not_federated_and_gets_register_reply",
            ),
            _node(
                "tests/behavior/test_publish_scenarios.py",
                "test_registered_user_without_accepted_subscription_does_not_publish",
            ),
        ),
    ),
    ContentContractEntry(
        "gateway_failure_no_false_success",
        "failure_isolation",
        "typed_case",
        (
            _node(
                "tests/behavior/test_publish_scenarios.py",
                "test_gateway_publish_failure_does_not_persist_false_success",
            ),
            _node(
                "tests/test_discord_publish_flow.py",
                "test_gateway_publish_failure_does_not_store_false_success_mapping",
            ),
        ),
    ),
    ContentContractEntry(
        "local_post_comment_publish",
        "create",
        "named_scenario",
        (
            _node(
                "tests/behavior/test_local_community_publish_scenarios.py",
                "test_registered_user_thread_starter_in_local_community_publishes_post",
            ),
            _node(
                "tests/behavior/test_local_community_publish_scenarios.py",
                "test_discord_reply_in_local_community_thread_"
                "publishes_comment_with_parent_mapping",
            ),
        ),
    ),
    ContentContractEntry(
        "reply_parent_resolution",
        "reply_mapping",
        "named_scenario",
        (
            _node(
                "tests/test_phase4_reply_preservation.py",
                "test_phase4_reply_to_starter_references_mirror_starter",
            ),
            _node(
                "tests/test_phase4_reply_preservation.py",
                "test_phase4_reply_to_mirrored_message_references_mirror_delivery",
            ),
            _node(
                "tests/test_discord_publish_flow.py",
                "test_thread_reply_uses_parent_comment_object_id_when_available",
            ),
        ),
    ),
    ContentContractEntry(
        "unknown_parent_fallback",
        "reply_mapping",
        "named_scenario",
        (
            _node(
                "tests/test_phase4_reply_preservation.py",
                "test_phase4_reply_to_unknown_message_mirrored_flat",
            ),
            _node(
                "tests/behavior/test_publish_scenarios.py",
                "test_remote_subscription_reply_semantics_preserve_nested_and_unknown_fallback",
            ),
        ),
    ),
    ContentContractEntry(
        "discord_edit_propagation",
        "edit",
        "named_scenario",
        (
            _node(
                "tests/test_phase8_edit_delete_sync.py",
                "test_source_message_edit_propagates_to_mirrors_and_ap",
            ),
            _node(
                "tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py",
                "test_host_and_local_subscriber_comment_edits_update_other_surfaces",
            ),
        ),
    ),
    ContentContractEntry(
        "discord_delete_propagation",
        "delete",
        "named_scenario",
        (
            _node(
                "tests/test_phase8_edit_delete_sync.py",
                "test_source_message_delete_removes_mirrors_and_sends_ap_delete",
            ),
            _node(
                "tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py",
                "test_local_discord_deletes_mark_non_source_surfaces",
            ),
        ),
    ),
    ContentContractEntry(
        "inbound_update_delete",
        "edit_delete_inbound",
        "named_scenario",
        (
            _node(
                "tests/behavior/test_local_community_edit_delete_scenarios.py",
                "test_inbound_local_community_post_update_edits_discord_starter",
            ),
            _node(
                "tests/behavior/test_local_community_edit_delete_scenarios.py",
                "test_inbound_local_community_comment_delete_marks_discord_message_deleted",
            ),
            _node(
                "tests/test_phase8_edit_delete_sync.py",
                "test_inbound_comment_update_edits_all_discord_deliveries",
            ),
            _node(
                "tests/test_phase8_edit_delete_sync.py",
                "test_inbound_post_delete_marks_all_thread_starters_deleted",
            ),
        ),
    ),
    ContentContractEntry(
        "partial_mutation_failure_isolated",
        "failure_isolation",
        "named_scenario",
        (
            _node(
                "tests/test_phase8_edit_delete_sync.py",
                "test_mirror_edit_failure_does_not_block_ap_update",
            ),
            _node(
                "tests/test_phase8_edit_delete_sync.py",
                "test_mirror_delete_failure_does_not_block_ap_delete",
            ),
            _node(
                "tests/behavior/test_local_community_stage5_participant_edit_delete_scenarios.py",
                "test_partial_local_discord_failure_does_not_block_healthy_targets",
            ),
        ),
    ),
    ContentContractEntry(
        "bidirectional_mirror_publish",
        "bidirectional",
        "named_scenario",
        (
            _node(
                "tests/test_phase9_bidirectional_mirror_messages.py",
                "test_source_thread_message_publishes_to_ap_and_mirrors",
            ),
            _node(
                "tests/test_phase9_bidirectional_mirror_messages.py",
                "test_mirror_thread_message_publishes_to_ap_and_fanout",
            ),
        ),
    ),
    ContentContractEntry(
        "mirror_loop_prevention",
        "dedup",
        "generated_assurance",
        (
            _node(
                "tests/test_phase9_bidirectional_mirror_messages.py",
                "test_mirror_message_does_not_loop_back_via_fanout",
            ),
            _node(
                "tests/test_phase9_bidirectional_mirror_messages.py",
                "test_mirror_message_dedup_on_reconnect",
            ),
            _node(
                "tests/test_end_to_end_dedup_flow.py",
                "test_duplicate_inbound_delivery_returns_duplicate_without_side_effects",
            ),
        ),
    ),
    ContentContractEntry(
        "out_of_order_retry",
        "dedup",
        "generated_assurance",
        (
            _node(
                "tests/test_end_to_end_dedup_flow.py",
                "test_out_of_order_comment_receipt_becomes_deferred_and_retries_successfully",
            ),
        ),
    ),
)
