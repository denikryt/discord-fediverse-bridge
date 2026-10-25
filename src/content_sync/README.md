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

`resolve_community_mutation_source(session, MessageRef, ...)` extends the
source resolver to authored Update/Delete: immutable AP publication, physical
mapping, live forum ownership, registered user, exact source coordinates and
canonical Local/Remote row must all agree. It returns no authorization when
only an old numeric source message ID is known; the Personal Feed keeps its
own separate legacy mutation projection.

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

## Stage 04 physical Create coordination (Checkpoint 59)

`platform_fanout.PlatformFanoutCoordinator` and the
`PlatformDeliveryIntentRepository` own the single immutable physical Create
request for each canonical target surface. Reservation and the fully materialized
DTO commit **before** a typed adapter call; a successful returned thread/starter
or message receipt commits its physical mapping in the same SQLite writer
transaction as `delivered`. Dispatching rows that lose an answer are
`unresolved` and are never blindly replayed. The original target and verified
orphan receipt remain in the intent if forum ownership changes in flight.
The coordinator selects the adapter via connector registry and never imports
Discord. A blocked child has no executable request until its exact parent is
verified. A confirmed parent receipt wakes its blocked descendants from the
canonical SQLite content snapshot; the SDK-ready Personal Feed Discord worker
also runs `PlatformFanoutCoordinator.recover_pending()` on startup and its
existing periodic cadence. Only definitely unsent, authorized `pending`
requests enter transport; expired `dispatching` becomes `unresolved` and
cannot be replayed merely because the worker restarted. Durable physical Update/Delete recovery is owned separately by
`platform_mutations.PlatformPhysicalMutationCoordinator` and uses the four
existing persisted physical-copy rows, never a fabricated Create intent. A
canonical verified revision or tombstone is committed before any I/O; the
copy's CAS `applied_revision` advances only after an acknowledged typed edit
(or tombstone edit) result. A lost response, expired dispatch, or unknown
transport effect remains `unresolved` across restart and cannot be blindly
retried. The app supplies the connector-specific Discord content renderer and
permission evaluator; the shared coordinator imports no Discord SDK.
Both the Create recovery and mutation recovery drain in the existing
Discord-ready scheduler, preserving shutdown order. Exclusive adapter Create
dispatch permits are a subsequent Stage 04 checkpoint.
