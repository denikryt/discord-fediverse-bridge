# local_communities

## Purpose

This package implements Discord forum channels exposed as local ActivityPub Group actors, including remote-subscriber state, canonical local-community mappings, and relay behavior.

## Responsibility

Local community runtime behavior, remote-subscriber follow acceptance, canonical local-content mappings, Discord surface lookups, and relay fanout.

## Not responsible for

Remote Lemmy subscription binding, gateway WebFinger routing, or generic content helpers.

## Primary entry points

`service.py`, `runtime.py`, `discord_fanout.py`, `federation_fanout.py`, `activitypub_renderers.py`, `delivery_mapping.py`, `inbound_mapping.py`, `reply_mapping.py`.

## Important tables or payloads

`local_communities`, `remote_subscribers`, `local_subscribers`, canonical `local_community_threads` / `local_community_messages`, per-surface `local_community_thread_surfaces` / `local_community_message_surfaces`, relay tables, and `local.follow_requested` events.

## Stage 4 local subscriber boundary

Stage 4 makes active local subscriber forums create-capable community participants. A local-subscriber-originated post/comment creates one canonical activity, one source local-subscriber surface, host/sibling Discord surfaces, and the existing local-community ActivityPub publish. Participant-wide edit/delete propagation is still deferred to Stage 5.
