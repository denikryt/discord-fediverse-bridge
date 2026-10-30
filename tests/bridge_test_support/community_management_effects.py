"""Observable effect snapshots for community-management contract tests."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CommunityManagementObserved:
    """Capture public result, persisted state, and action-local audits."""

    applied: bool
    reason: str
    display_name: str | None
    summary: str | None
    status: str | None
    audit_events: tuple[tuple[str, str, str | None], ...]


def collect_community_management_effects(
    *, database: object, result: object, slug: str, audit_offset: int
) -> CommunityManagementObserved:
    """Collect observable effects after one real operation execution."""

    community = database.local_communities.get_local_community_by_slug(slug)
    rows = database.management_audit_events.list_oldest_first()[audit_offset:]
    return CommunityManagementObserved(
        applied=result.applied,
        reason=result.reason,
        display_name=getattr(community, "display_name", None),
        summary=getattr(community, "summary", None),
        status=getattr(community, "status", None),
        audit_events=tuple((row.action, row.result, row.reason_code) for row in rows),
    )
