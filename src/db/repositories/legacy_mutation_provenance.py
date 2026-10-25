"""Resolve original legacy authors from independent durable Create evidence.

Never derive ownership from an incoming mutation, object URL or a copy role.
This resolver runs inside the canonical writer transaction; its identity-only
result is a validation input, never a fabricated historical content snapshot.
"""
from __future__ import annotations

import json
from sqlalchemy import select

from ...models import PublishedActivityObject, LocalCommunityRelaySourceActivity


class LegacyMutationProvenanceRequired(ValueError):
    """Require operator inspection when original ownership cannot be proven."""


def source_identity(session, row, *, domain: str, kind: str,
                    community_actor_id: str, local_community_id: int | None = None) -> str:
    """Return existing snapshot or independently proven original AP identity.

    Only pristine legacy rows may bootstrap. A missing snapshot on a row that
    already advanced is corruption, never permission to replace its author.
    Every matching original evidence record must agree on one nonempty actor.
    """
    if row.current_content_json is not None:
        return row.current_content_json
    if row.content_revision != 0 and row.deleted_at is None:
        raise LegacyMutationProvenanceRequired('legacy source provenance requires operator review: missing revised snapshot')
    authors = set()
    published = session.scalar(select(PublishedActivityObject).where(
        PublishedActivityObject.object_id == row.ap_object_id))
    if published is not None:
        if (published.activity_id != row.ap_activity_id or published.kind != kind
            or published.publication_context != 'community'
            or published.community_actor_url != community_actor_id):
            raise LegacyMutationProvenanceRequired('legacy source provenance conflicts with original publication')
        authors.add(published.actor_url)
    if domain == 'local':
        sources = session.scalars(select(LocalCommunityRelaySourceActivity).where(
            LocalCommunityRelaySourceActivity.local_community_id == local_community_id,
            LocalCommunityRelaySourceActivity.source_object_ap_id == row.ap_object_id,
            LocalCommunityRelaySourceActivity.operation == 'create',
        )).all()
        for source in sources:
            activity = source.source_activity_json
            obj = activity.get('object') if isinstance(activity, dict) else None
            # The durable record must be the original Create, including its
            # activity, object and author; a later Update is not independent proof.
            if (source.object_kind != kind or source.source_activity_id != row.ap_activity_id
                or not isinstance(activity, dict) or activity.get('type') != 'Create'
                or activity.get('id') != row.ap_activity_id
                or activity.get('actor') != source.origin_remote_actor_id
                or not isinstance(obj, dict) or obj.get('id') != row.ap_object_id
                or obj.get('attributedTo') != source.origin_remote_actor_id):
                raise LegacyMutationProvenanceRequired('legacy source provenance conflicts with original Create')
            authors.add(source.origin_remote_actor_id)
    if len(authors) != 1 or not all(isinstance(a, str) and a.strip() for a in authors):
        raise LegacyMutationProvenanceRequired('legacy source provenance requires operator review: original author unknown or conflicting')
    return json.dumps({'apObjectId': row.ap_object_id, 'kind': kind,
                       'authorActorId': authors.pop()}, sort_keys=True)
