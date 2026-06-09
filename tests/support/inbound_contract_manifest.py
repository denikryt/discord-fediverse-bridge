"""Machine-readable rule ownership for inbound ActivityPub assurance."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal
Classification=Literal['typed_case','named_scenario','technical_contract']
@dataclass(frozen=True,slots=True)
class InboundContractEntry:
 rule_id:str; family:str; classification:Classification; node_prefixes:tuple[str,...]
INBOUND_CONTRACT_ENTRIES=(
 InboundContractEntry('accepted_post_comment','handler_outcome','named_scenario',('tests/behavior/test_inbound_scenarios.py::test_accepted_subscription_inbound_post_creates_discord_thread_and_receipt','tests/behavior/test_inbound_scenarios.py::test_accepted_subscription_inbound_comment_creates_discord_message_and_receipt')),
 InboundContractEntry('unsubscribed_skip','policy_routing','named_scenario',('tests/behavior/test_inbound_scenarios.py::test_no_accepted_subscription_inbound_post_is_skipped','tests/behavior/test_unsubscribed_inbound_activity_skip.py::test_unsubscribed_remote_post_create_is_skipped_before_thread_creation','tests/behavior/test_unsubscribed_inbound_activity_skip.py::test_unsubscribed_remote_comment_without_mapped_context_is_skipped')),
 InboundContractEntry('duplicate_delivery','dedup','named_scenario',('tests/behavior/test_inbound_scenarios.py::test_duplicate_delivery_id_returns_idempotent_duplicate_without_side_effects','tests/test_phase6_dedup_hardening.py::test_replayed_inbound_post_returns_skipped','tests/test_phase6_dedup_hardening.py::test_replayed_inbound_comment_returns_skipped')),
 InboundContractEntry('echo_prevention','dedup','named_scenario',('tests/behavior/test_inbound_scenarios.py::test_discord_originated_echo_is_skipped_without_creating_duplicate','tests/test_phase6_dedup_hardening.py::test_inbound_post_from_own_actor_is_suppressed_before_handler')),
 InboundContractEntry('deferred_parent_retry','backfill','named_scenario',('tests/behavior/test_inbound_scenarios.py::test_comment_before_parent_mapping_becomes_deferred_then_retries_processed','tests/behavior/test_inbound_comment_backfill.py::test_inbound_comment_deferred_when_post_fetch_fails')),
 InboundContractEntry('comment_backfill','backfill','named_scenario',('tests/behavior/test_inbound_comment_backfill.py::test_inbound_comment_creates_post_thread_when_post_not_yet_mapped','tests/behavior/test_inbound_comment_backfill.py::test_inbound_comment_only_backfills_channels_without_existing_delivery')),
 InboundContractEntry('failed_delivery_receipt','handler_outcome','named_scenario',('tests/behavior/test_inbound_scenarios.py::test_discord_target_failure_marks_inbound_receipt_failed',)),
 InboundContractEntry('local_follow_accept','local_community','named_scenario',('tests/behavior/test_local_community_inbound_scenarios.py::test_remote_follow_to_local_community_persists_remote_subscriber_and_sends_accept','tests/behavior/test_local_community_inbound_scenarios.py::test_repeated_remote_follow_resends_accept_and_refreshes_request_details')),
 InboundContractEntry('local_unfollow_idempotency','local_community','named_scenario',('tests/behavior/test_local_community_inbound_scenarios.py::test_remote_unfollow_to_local_community_removes_remote_subscriber','tests/behavior/test_local_community_inbound_scenarios.py::test_duplicate_remote_unfollow_to_local_community_is_idempotent')),
 InboundContractEntry('local_content_follower_gate','policy_routing','named_scenario',('tests/behavior/test_local_community_inbound_scenarios.py::test_remote_follower_top_level_post_creates_new_discord_thread','tests/behavior/test_local_community_inbound_scenarios.py::test_remote_non_follower_top_level_post_is_skipped')),
 InboundContractEntry('shared_group_mapping','mapping_receipt','named_scenario',('tests/test_phase5_inbound_ap_shared_groups.py::test_phase5_inbound_post_creates_thread_group_and_deliveries','tests/test_phase5_inbound_ap_shared_groups.py::test_phase5_inbound_comment_reply_chain_resolves_to_prior_delivery')),
 InboundContractEntry('receipt_outcome_atomicity','outcome_schema','typed_case',('tests/test_inbound_activity_outcomes.py::test_repository_updates_status_outcome_and_detail_together','tests/test_inbound_activity_outcomes.py::test_retry_start_clears_stale_terminal_outcome')),
 InboundContractEntry('internal_read_api','technical_api','technical_contract',('tests/test_internal_fedify_api.py::test_internal_fedify_reads_are_authenticated_and_no_store','tests/test_internal_fedify_api.py::test_internal_fedify_resolves_community_subscribers_objects_and_mappings')),
)
