# content_sync

## Purpose

This package contains reusable content publishing, reference, reply, persistence, edit, and delete helpers. It does not decide whether an event belongs to remote subscription mode or local community mode.

## Responsibility

Build outbound publish/update/delete requests, resolve reply references, and persist shared object/mapping state.

## Not responsible for

Runtime selection, Discord delivery, gateway routes, or ActivityPub signing.

## Primary entry points

`outbound_publish.py`, `inbound_references.py`, `reply_mapping.py`, `edit_delete.py`, `persistence.py`, `surface_resolution.py` (typed current forum owner source evidence).

## Important tables or payloads

`message_mappings`, `published_activity_objects`, community group tables, and gateway publish/update/delete payloads.

## local subscriber synchronization dependency audit

Legacy `inbound_references.build_message_reference()` defers loading
`discord.py` until a legacy physical send actually requests an SDK reference.
Importing reusable content helpers and CommunityRuntime no longer loads the
Discord SDK. The remaining legacy SDK-based reference/edit/delete operations
must be removed after their callers are migrated; they are not Platform API.

## Stage 04 source ownership

`surface_resolution.resolve_forum_source(session, ChannelRef)` returns a frozen
owner candidate only for the exact connector, forum channel and verified guild.
The owning Local/Remote Community lifecycle is checked on the same read. A
consumer must revalidate the claim in its own writer transaction before a
canonical admission or physical send; a candidate is not a persistent permit.

`MessageMapping` and `PublishedActivityObject` distinguish the source's
immediate message container from its parent forum surface. New neutral callers
look up exact `MessageRef`s. The old numeric-only Discord read methods reject
fully typed sources rather than treating a missing container as a wildcard.
Stage 04 community Create uses a single typed ingress: source identity and
current surface ownership are checked before and during the SQLite writer
transaction. `canonical_admission.py` atomically persists the community post
or comment, its original typed physical receipt, `MessageMapping`,
`PublishedActivityObject`, and signed delivery audience. The Fedify gateway's
`/render-community` route only renders deterministic Page/Note/Create bytes;
legacy `/publish` endpoints reject calls rather than sending before persistence.
A verified current Remote Group Follow grants a direct User Actor recipient;
Local Community Group Announce destinations are independently snapshot-bound
to an accepted Follow incarnation in the same writer commit. Revocation is
checked again before transport. Failed renders and invalid reply parents
leave no canonical graph or signed network send.
