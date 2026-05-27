"""Community sync orchestration package.

This package owns the shared-community-sync feature: one source event fans out
into an AP publish and into all subscribed Discord channels via CommunityRuntime.

Phase 0: CommunityRuntime is a thin pass-through stub that delegates to the
existing ContentPublishService and activitypub_handlers. No new logic yet.
Phase 2+: CommunityRuntime will own thread/message group creation and Discord fanout.
"""
