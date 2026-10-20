# local_communities

## Purpose

This package implements Discord forum channels exposed as local ActivityPub Group actors, including remote-subscriber state, canonical local-community mappings, and relay behavior.

## Responsibility

Local community runtime behavior, remote-subscriber follow acceptance, canonical local-content mappings, Discord surface lookups, and relay fanout.

## Not responsible for

Remote Lemmy subscription binding, gateway WebFinger routing, or generic content helpers.

## Primary entry points

`service.py`, `runtime.py`, `results.py`, `discord_create.py`, `inbound_create.py`, `mutations.py`, `followers.py`, `discord_surface_fanout.py`, `discord_fanout.py`, `federation_fanout.py`, `rendering.py`, `activitypub_renderers.py`, `delivery_mapping.py`, `inbound_mapping.py`, and `reply_mapping.py`.

## Important tables or payloads

`local_communities`, `remote_subscribers`, `local_subscribers`, canonical `local_community_threads` / `local_community_messages`, per-surface `local_community_thread_surfaces` / `local_community_message_surfaces`, relay tables, and `local.follow_requested` events.

## Local subscriber participant boundary

Stage 5 makes active local subscriber forums create/edit/delete-capable community participants. A local-subscriber-originated post/comment creates one canonical activity, one source local-subscriber surface, host/sibling Discord surfaces, and the existing local-community ActivityPub publish. Later edits/deletes from host or active local-subscriber surfaces resolve through the persisted surface rows, update/delete the canonical AP object through the gateway, and mutate every non-source local Discord surface.

## User moderation boundary

Local-community host and active local-subscriber sources apply global bans first and then the canonical community-scoped ban before ActivityPub publishing, canonical-row creation, or Discord fanout. Rejection reasons remain on the source Discord surface. Registered local authors are attributed by their stable `user@bridge-authority` handle across all participant surfaces.

## Source-level relay identity and retry admission (Stage 03)

One inbound relay source owns one immutable community `Announce.id`, shared by all
recipient delivery rows. Before transport, each non-delivered target attempts an
atomic `BEGIN IMMEDIATE` admission which checks the current accepted subscriber,
endpoint and federation policy, reserves the shared ID, and advances an attempt
generation. The physical HTTP request uses a frozen inbox snapshot; a callback
must match both that snapshot and the admitted generation before persisting a
result. Historical conflicting source IDs halt retries without rewriting
already published identities. Successful targets never replay solely because
an Actor's inbox changes.

### Stage 03 canonical receipt persistence

Host thread/comment canonical writes and their host Discord surface receipts
are now idempotent under a `Database.immediate_session()` transaction. Repeated
identical AP/Discord receipts reuse the existing mapping; changed activity,
object, parent or physical IDs fail with a conflict before altering saved
identity. Generic inbound comment mappings follow the same receipt identity
rule, permitting recovery when an earlier handler committed its canonical
message but stopped before generic mapping persistence. The domain ingress
must still verify actor provenance **before** invoking sibling fanout/relay.

## Inbound duplicate admission (Stage 03, partial)

Before an already mapped remote post/comment can trigger Discord sibling
fanout or ActivityPub relay, the inbound handler verifies the current accepted
remote subscriber, canonical AP activity/object/community/parent identity and
the previously retained immutable source. Legacy messages without a retained
source take the searchable T8 fail-closed branch. Only an identical verified
comment can restore a missing generic AP mapping from its persisted host
Discord receipt. **Raw source JSON is not author authentication**: the
independent nested-Create verification at the Fedify boundary remains a release
blocker; tests here establish database replay consistency, not HTTP authorship.

### Sibling Discord receipts (Stage 03)

Sibling forum-thread and comment surfaces now use the same SQLite
`BEGIN IMMEDIATE` writer boundary as canonical host receipts. Concurrent
handlers may reuse an identical physical receipt, but a different thread,
starter, comment, parent, forum, subscriber owner or role fails before a
mapping can be overwritten. A message surface must also belong to the
canonical parent thread carried by its parent physical surface. There is no
new queue or database schema.

### Stage 03 normalized Local Community source (Checkpoints 31–32)

`LocalCommunityRuntime.handle_normalized_thread_create()` and
`handle_normalized_message_create()` now accept verified `ThreadCreated`/
`MessageView` and `MessageCreated` snapshots, respectively. Host and current
active subscriber sources are resolved against the stored Discord forum claim,
confirmed guild and community lifecycle before publication; Fluxer IDs cannot
be projected into legacy Discord columns. Canonical AP publication still uses
`ContentPublishService`, and the existing host/subscriber surface repositories
own receipt identity. A subscriber-authored source is **not** stored as a host
surface. Typed `LocalCommunityDiscordFanout` delivers target-specific copies,
including nested replies whose parent is present on the destination.
An already mapped starter of a different physical thread is explicitly
rejected, not treated as an unknown parent and flattened to the root.

A repeated physical copy must not be mistaken for an originating subscriber:
local subscriber source and mirrored subscriber both have role
`local_subscriber`. A replay can repair missing copies only if the persisted
`PublishedActivityObject` confirms the actual author, source forum/message,
original title and body. A mere matching Discord ID or source role is not
sufficient. Production forum Create callbacks now use the single normalized
`PlatformEventIngress` route; both host and subscriber authorship are checked
by `LocalCommunityRuntime` before canonical publication, and no parallel SDK
Create callback publishes a second activity. Remaining inbound ActivityPub
physical writes and edits/deletes are separate Stage 03 migration work.
