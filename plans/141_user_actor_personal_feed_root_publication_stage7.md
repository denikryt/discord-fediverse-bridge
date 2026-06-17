# 141 — User Actor Personal-Feed Root Publication (Stage 7)

## Problem / Goal

Stage 6 persists and classifies one active ordinary Discord guild text channel for a registered User Actor, but `BridgeBot.on_message` intentionally returns without publishing. Stage 7 must turn one eligible owner-authored root message in that bound channel into one durable public ActivityPub `Note`, preserve one canonical Create/object identity across restart and follower retries, and expose a stable public object URL.

The current generic Discord publication path is community-shaped end to end:

- `ContentPublishService` requires `community_actor_url` and builds either a post `Page` or reply `Note`;
- `PublishContentRequest` and the gateway `/internal/publish` contract require a remote/local community;
- `buildPublishCreateActivity()` places that community in `audience` and `to`;
- `MessageMapping.community_actor_url` and `PublishedActivityObject.community_actor_url` are non-null;
- `persist_publish_artifacts()` assumes the gateway has already completed the community delivery before it atomically stores canonical state and the fixed follower snapshot.

A personal-feed root has no community owner, is not a reply, and must not fabricate a Group URL to satisfy those assumptions. This stage therefore introduces a typed publication context and a dedicated user-profile publication path while preserving all community publication semantics.

## Expected Behavior

1. `BridgeBot.on_message` continues to ignore bot-authored messages and events outside allowed guilds. For an ordinary guild text channel with an active personal-feed binding, it routes the event to the personal-feed root publisher before the existing thread/forum path.
2. The publisher accepts only a root `discord.Message` whose guild/channel exactly match the active binding and whose `message.author.id` equals the binding owner’s persisted Discord user ID. A non-owner message returns `ignored/non_owner` without a reply, reaction, mapping, delivery row, gateway request, log containing message content, or any other bridge side effect.
3. An eligible owner message publishes the complete Discord `message.content` as one public ActivityPub `Note`. The bridge does not apply Mastodon’s local 500-character composition limit: that limit is an instance UI/API policy, not an ActivityPub `Note.content` protocol limit. Stage 7 does not publish Discord attachments, embeds, stickers, polls, or components; an empty textual message is ignored because there is no supported payload to federate.
4. The object is authored and signed by the existing User Actor. The Create and Note address ActivityStreams Public plus the actor’s followers collection; no community appears in `audience`, `to`, `cc`, persistence, or delivery routing.
5. The object ID remains under the existing dereferenceable user-object route, for example `https://bridge.example/users/alice/post/<stable-id>`. `Note.url` equals that canonical object URL. The route returns the complete stored post after restart.
6. The gateway only renders the canonical Create/Note and returns its JSON. It does not perform follower fanout inline. Python commits the `MessageMapping`, `PublishedActivityObject`, typed personal-feed context, fixed follower snapshot, and ready `UserActorOutboundSource` in one SQLite `BEGIN IMMEDIATE` transaction, then wakes the existing Stage 4/5 delivery worker.
7. Duplicate delivery of the same Discord `MESSAGE_CREATE` event resolves to the already-persisted publication and does not call the gateway again, create a second object, resnapshot followers, or emit a second follower delivery source.
8. No Discord thread is created, adopted, or mapped by root publication.
9. No historical scan or backfill occurs at startup. Only live eligible events are published.
10. Gateway failure before canonical JSON is returned leaves no mapping, published object, or follower-delivery source. The Discord message remains eligible for a later duplicate/replayed gateway event. A failure after the gateway has rendered but before Python commits is handled by deterministic create identity described below, so retry reuses the same IDs rather than generating a second remote object.

## Confirmed External Contracts

- ActivityPub permits a client-to-server or server-to-server `Create` carrying an object and requires the object to have an `id` when the server is not assigning one. Public addressing uses the ActivityStreams Public IRI; actor followers collections are normal delivery targets. This is generic ActivityPub behavior, not Mastodon-specific.
- Mastodon’s default 500-character status limit applies to statuses authored through that Mastodon server. It is not a normative limit on remote ActivityPub `Note.content`, so the bridge must not truncate a Discord post to that value.
- Discord exposes message content through `MESSAGE_CREATE` only when the Message Content privileged intent is configured/approved where required. The repository already enables `intents.message_content`; deployment documentation must keep that prerequisite explicit.

Primary references to verify during implementation:

- https://www.w3.org/TR/activitypub/
- https://docs.joinmastodon.org/spec/activitypub/
- https://docs.joinmastodon.org/user/posting/
- https://docs.discord.com/developers/events/gateway
- https://docs.discord.com/developers/resources/message

## Architecture

### 1. Typed publication context

Replace the implicit “every object belongs to a community” invariant with an explicit discriminator on both durable canonical records:

```text
publication_context = "community" | "personal_feed"
community_actor_url = URL for community context, NULL for personal_feed
```

Apply this to `PublishedActivityObject` and `MessageMapping`. Add database checks equivalent to:

```sql
CHECK (
  (publication_context = 'community' AND community_actor_url IS NOT NULL)
  OR
  (publication_context = 'personal_feed' AND community_actor_url IS NULL)
)
```

`discord_channel_id` on a personal-feed root stores the bound text channel ID; `discord_message_id` stores the root message ID. Existing community rows migrate to `publication_context='community'` with their current non-null URL. No fake actor, sentinel URL, empty string, or overloaded `kind` is allowed.

This foundation is required now because Stage 7 must persist a non-community object. Deferring it would either lose truthful ownership or require an incompatible cleanup migration after personal-feed rows already exist. It also gives Stage 8 an unambiguous context discriminator without implementing any Stage 8 reply/thread behavior.

`kind` remains the canonical object shape (`post` or `comment`). A personal-feed root uses `kind='post'` even though its ActivityStreams type is `Note`; existing route naming and Stage 9 outbox categorization already treat root authored objects as posts. Do not introduce a new mutation/reply state in this stage.

### 2. Deterministic create reservation and idempotency

The current gateway generates Create/object IDs from `Date.now()` and randomness before Python persists them. That is insufficient for a Discord event that can be replayed after an ambiguous HTTP/DB failure. Stage 7 adds caller-supplied create identity for the personal-feed path:

```text
operation_id = sha256("personal_feed_root\0" + guild_id + "\0" + channel_id + "\0" + message_id)
object_id    = <origin>/users/<username>/post/<operation_id>
activity_id  = <origin>/users/<username>/activities/create/post/<operation_id>
```

The gateway request includes `operationId`, `objectId`, and `activityId`; the gateway verifies that all three match the normalized request identity and canonical actor username before constructing the Create. This is the same security property used by the Stage 5 mutation identity check: Python chooses stable operation identity, while the signing gateway rejects inconsistent IDs.

The operation identity deliberately excludes mutable message content. Discord can deliver an update after create, but edits belong to Stage 8; changing content during a duplicate root event must not create another object. The first committed create remains authoritative until Stage 8 introduces Update semantics.

Before gateway dispatch, Python checks `MessageMapping(source_platform='discord', source_id=str(message.id))`. If present, it validates that the mapping is a personal-feed publication for the same channel and actor, then returns `already_published`. A conflicting row is a fail-closed data-integrity error and must not be overwritten.

A new durable pre-dispatch create state/lease is not added. The deterministic gateway contract removes duplicate identity after ambiguous retries, and the final SQLite transaction remains the first durable local write. Full generic create recovery coordination is not required by any current Stage 7 scenario and would duplicate Stage 5’s mutation machinery without evidence that the gateway endpoint itself has a durable side-effect before follower delivery. The gateway endpoint for this path must remain render-only; therefore retrying the same request is safe.

### 3. Dedicated personal-feed publication service

Add a narrow `PersonalFeedPublisher` (or equivalently named service) rather than adding personal-feed branches throughout `ContentPublishService`.

Responsibilities:

- re-resolve the current binding at execution time;
- verify guild/channel/owner and supported textual payload;
- resolve the registered `User` from the binding’s `user_id`;
- enforce the global Discord-user ban check fail-closed, without applying a community-specific ban;
- derive deterministic operation/object/activity IDs;
- perform the pre-dispatch duplicate/conflict lookup;
- call the new gateway render endpoint;
- atomically persist canonical artifacts and the fixed follower snapshot;
- wake the existing `UserActorDeliveryWorker` only after commit;
- return a structured `PublishResult` consumed by the Discord adapter and tests.

`PersonalFeedService` remains the control-plane owner for bindings. It must not acquire publication, gateway, or delivery responsibilities. `ContentPublishService` remains the community publication owner. This keeps authorization-independent binding transactions separate from federation create transactions.

The publisher receives `Database`, `FedifyGatewayClient`, `BridgePolicyService`/`UserBanService`, settings/origin needed for deterministic URLs, and the existing delivery worker. It owns no long-running task and therefore requires no shutdown hook.

### 4. Discord admission and data flow

`BridgeBot.on_message` retains this order:

```text
bot/self author guard
  -> normal guild text-channel predicate
  -> resolve active personal-feed binding under allowed-guild policy
  -> if binding exists: route exclusively to PersonalFeedPublisher and return
  -> existing Discord Thread/forum routing unchanged
```

The router gains a single forwarding method such as:

```python
async def handle_personal_feed_root(*, message, binding) -> PublishResult
```

The router does not infer ownership from Discord alone; the publisher validates the supplied binding again against persistence to close the race where an unbind/move commits between classification and dispatch.

Non-owner roots are silent. Owner failures are logged without message content. Stage 7 does not add a Discord reply for federation failure because the umbrella requires non-owner silence and does not define a user-visible failure protocol; emitting bot messages into the feed could itself be mistaken for content and complicate idempotency. Operational diagnostics use IDs and sanitized exception classes only.

### 5. Gateway personal-feed render contract

Add a distinct internal endpoint/client method, for example:

```text
POST /internal/publish-personal-feed
{
  "actorUsername": "alice",
  "operationId": "<sha256>",
  "activityId": "https://.../users/alice/activities/create/post/<sha256>",
  "objectId": "https://.../users/alice/post/<sha256>",
  "bodyMarkdown": "complete Discord content"
}
```

Response:

```text
{
  "activityId": "...",
  "objectId": "...",
  "activityJson": { canonical Create(Note) }
}
```

The resulting object/activity shape is:

```json
{
  "type": "Create",
  "id": "https://.../users/alice/activities/create/post/<operation-id>",
  "actor": "https://.../actors/alice",
  "to": ["https://www.w3.org/ns/activitystreams#Public"],
  "cc": [
    "https://.../actors/alice",
    "https://.../actors/alice/followers"
  ],
  "object": {
    "type": "Note",
    "id": "https://.../users/alice/post/<operation-id>",
    "url": "https://.../users/alice/post/<operation-id>",
    "attributedTo": "https://.../actors/alice",
    "to": ["https://www.w3.org/ns/activitystreams#Public"],
    "cc": ["https://.../actors/alice/followers"],
    "content": "<sanitized HTML rendered from the complete Markdown source>",
    "source": {"mediaType": "text/markdown", "content": "complete Discord content"},
    "published": "..."
  }
}
```

Do not set `audience` to a fabricated community. Do not send the activity inside this endpoint. The Stage 4/5 worker signs and delivers the returned canonical JSON to the fixed accepted-follower endpoint snapshot.

The existing community `/internal/publish` endpoint and `buildPublishCreateActivity()` retain their current behavior and random IDs. Generalizing them to optional community data is rejected because it would make invalid combinations representable and risks changing remote/local community addressing. A dedicated builder/endpoint keeps the new invariant explicit.

### 6. Atomic persistence and follower delivery

Generalize `persist_publish_artifacts()` into a typed persistence operation or add a sibling `persist_personal_feed_publish_artifacts()` backed by one shared private implementation. The Stage 7 transaction must:

1. load and verify the `User` by `user_id`, actor URL, and username;
2. re-check no `MessageMapping` already owns the Discord source ID;
3. insert `MessageMapping(publication_context='personal_feed', community_actor_url=NULL, channel/message IDs...)`;
4. insert `PublishedActivityObject(publication_context='personal_feed', community_actor_url=NULL, kind='post', title=NULL, body_markdown=full content, in_reply_to=NULL...)`;
5. snapshot currently accepted User Actor followers through `list_accepted_for_snapshot()`;
6. insert one ready `UserActorOutboundSource(source_kind='content_create', logical_version=1, activity_json=<exact gateway JSON>, published_object_id=...)` and its deduplicated per-endpoint follower rows;
7. commit once.

The snapshot semantics are unchanged from Stage 5: followers accepted after publication do not retroactively receive the post, removed followers remain part of an already-fixed source only according to the existing worker’s policy, and shared inbox endpoints are deduplicated by the established delivery repository.

A follower endpoint failure is post-commit, per-target, retryable, and does not alter the successful local publication. The worker lifecycle remains owned by `Runtime`; Stage 7 only wakes it.

### 7. Public dereferencing

Reuse the existing `/users/:username/post/:objectId` route and `published-objects` reconstruction path. Update the internal `PublishedObjectResponse` and gateway `PublishedObjectRecord` so `communityActorUrl` is nullable and `publicationContext` is explicit.

`buildPublishedActivityObjectJson()` must reconstruct a personal-feed `Note` without community `audience`/recipient fields and with `url` equal to `objectId`. Community `Page`/`Note` reconstruction remains byte-semantically compatible with existing tests.

`loadPublishedCreateActivityForRequest()` must likewise rebuild a personal-feed Create with the same addressing as the original gateway response. This is required because follower retries, remote dereferencing, and restart must all observe the same canonical activity, not a community-shaped reconstruction.

The User Actor outbox remains the existing empty placeholder; listing/pagination is Stage 9.

### 8. Migration and compatibility

SQLite cannot safely add the nullable/context checks to the existing tables in place. Add an explicit migration that rebuilds `message_mappings` and `published_activity_objects` using the current Stage 5 rebuild style:

- rename the old table;
- create the new metadata-defined table;
- copy all existing rows with `publication_context='community'` and unchanged values/IDs/timestamps/revision/reconciliation fields;
- recreate uniqueness and indexes;
- drop the old table;
- run inside the existing migration transaction.

Preserve primary keys because `user_actor_content_mutations.published_object_id` and outbound sources reference `published_activity_objects.id`. Temporarily disabling/re-enabling SQLite foreign-key enforcement must follow the repository’s established migration mechanism; tests must prove references survive.

Clean databases receive the final schema through SQLAlchemy metadata. No backfill from Discord history and no personal-feed rows are synthesized.

### 9. Security, privacy, and performance

- Trust boundary: Discord event identity and channel type are untrusted runtime inputs; binding/user ownership is reloaded from the DB before federation.
- Authorization: only exact owner ID publishes. Guild administrators, bridge super-admins, other registered users, webhooks, and bots do not inherit authorship.
- Content: use the existing Markdown-to-safe-HTML renderer. Never interpolate raw content into logs, exception messages, IDs, URLs, SQL, or route paths.
- Secrets: Python does not receive User Actor private keys. The existing gateway process remains the signing boundary used by the follower delivery worker. The new render endpoint needs no additional secret or deployment credential beyond the current internal gateway authentication/network boundary.
- Abuse limits: Stage 7 intentionally applies no Mastodon-local character cap, but it must reject an empty string and use the existing HTTP/gateway request-size limits. Do not add silent truncation. A deployment-wide explicit maximum would be a product contract not present in the umbrella plan.
- Performance: one indexed mapping lookup plus one short `BEGIN IMMEDIATE` transaction per eligible root. Network rendering occurs outside the DB transaction. Follower delivery remains asynchronous and per-target; no Discord callback waits for all followers.

## Alternatives Considered

### Reuse a fake community actor URL

Rejected. It would produce false ActivityPub audience semantics, contaminate persistence, misroute mutations in Stage 8, and require later destructive migration.

### Make `community_actor_url` nullable without a discriminator

Rejected. Null alone does not state whether a row is personal-feed, malformed legacy data, or a future publication mode. A discriminator plus check constraint makes invalid combinations fail at the database boundary.

### Generalize the existing community publish endpoint with nullable fields

Rejected. Community publication includes remote-community lookup/delivery and local-community Announce semantics. A broad optional request would combine unrelated responsibilities and make unsafe combinations easy to construct.

### Deliver followers directly in the gateway render request

Rejected. Stage 4/5 already owns durable per-target snapshots, retry, shared-inbox deduplication, and partial failure. Inline delivery would split one audience across two retry owners and could send before Python’s canonical mapping commits.

### Persist before calling the gateway

Rejected unless a full durable create reservation/recovery worker is added. A placeholder published row would be externally dereferenceable without canonical JSON and would require new states/leases outside the minimum Stage 7 need. Deterministic render-only gateway identity provides retry safety without exposing incomplete objects.

### Reuse random gateway IDs and rely only on mapping uniqueness

Rejected. A gateway response followed by a lost connection or failed DB commit could generate a second Create on retry. Deterministic IDs are required now to prevent unremovable duplicate public identities.

## Touched Files

- src/models.py
- src/db/migrations.py
- src/db/database.py
- src/db/repositories/message_mappings.py
- src/db/repositories/activitypub_objects.py
- src/content_sync/persistence.py
- src/discord_bot.py
- src/discord_event_router.py
- src/fedify_gateway_client.py
- src/internal_fedify_api.py
- src/app.py
- src/runtime.py
- fedify-gateway/src/types.ts
- fedify-gateway/src/federation-outbound.ts
- fedify-gateway/src/published-objects.ts
- fedify-gateway/src/python-bridge-client.ts
- fedify-gateway/src/server.ts
- tests/behavior/test_personal_feed_stage6_control_plane.py
- tests/behavior/test_publish_scenarios.py
- tests/behavior/test_user_actor_content_fanout_scenarios.py
- tests/test_migrations.py
- fedify-gateway/test/federation-outbound.test.ts
- fedify-gateway/test/server.test.ts
- docs/FEDERATION.md
- docs/DEPLOY.md
- docs/architecture/overview.md
- notes/known_issues.md

The exact gateway test filenames must follow the repository’s current test layout if they differ at implementation time. Files are listed because their present contracts are directly affected; unrelated documentation must not be edited.

## New Files

- src/personal_feed_publisher.py
- tests/behavior/test_personal_feed_stage7_root_publication.py

No new worker, retry scheduler, thread manager, inbound handler, or outbox module is introduced.

## Implementation Steps

1. Add failing migration tests that upgrade a populated Stage 6 schema containing community mappings, published objects, outbound-source references, and mutation references. Assert preserved IDs/data and the new context/check constraints.
2. Change `MessageMapping` and `PublishedActivityObject` to carry `publication_context` and nullable `community_actor_url`; implement the SQLite table rebuild and repository query/update compatibility.
3. Extend internal published-object DTOs and gateway Python client records with explicit context plus nullable community URL. Keep legacy/community payloads fully populated.
4. Add gateway tests for deterministic personal-feed identity verification and exact Create(Note) JSON. Implement the dedicated request type, builder, and render-only endpoint. Reject mismatched actor/operation/object/activity IDs and any non-canonical origin/path.
5. Update public object/create reconstruction to branch on `publicationContext`. Verify `/users/:username/post/:id` and create-activity routes return the same personal-feed recipients, source, content, actor, IDs, and `url` after a simulated restart.
6. Add failing runtime behavior tests through `BridgeBot.on_message`/`DiscordEventRouter` for owner publication, non-owner silence, empty text, duplicate events, unbind/move race, gateway failure, DB failure/retry, no thread creation, and isolation from community modes.
7. Implement `PersonalFeedPublisher` with binding revalidation, owner/global-ban checks, deterministic identity, duplicate/conflict handling, gateway call, and atomic persistence. Reuse the existing follower snapshot and delivery source contracts rather than creating a new fanout domain.
8. Wire one publisher instance in `src/app.py`, inject it into `DiscordEventRouter`, and route personal-feed root events from `BridgeBot.on_message`. Preserve all existing forum/thread ordering after the exclusive personal-feed branch.
9. Add regression tests proving existing remote-community and local-community Create JSON, persistence, delivery, edit/delete lookup, and reconstruction remain unchanged after the typed-context migration.
10. Update only responsible documentation: federation architecture for personal-feed Create(Note) addressing and typed context; deployment docs for Message Content intent and absence of attachment support. Record only newly verified limitations in `notes/known_issues.md`; do not add Stage 8/9 work as if implemented.
11. Run the validation matrix below and inspect the final diff to confirm no reply, inbound, edit/delete, thread, or outbox behavior entered Stage 7.

## Tests

### Runtime behavior scenarios

- **Owner root publication:** active binding + allowed guild + registered unbanned owner + normal text message -> one gateway render request, one mapping, one personal-feed published object, one content-create source, fixed follower deliveries, worker wake, no Discord reply/thread.
- **Complete content:** a message longer than 500 characters is stored completely; gateway source contains the exact text and rendered HTML contains the complete equivalent without truncation.
- **Non-owner silence:** registered/unregistered/admin/super-admin non-owner messages -> zero gateway, DB, Discord, and delivery effects.
- **Unsupported payload:** empty content with attachments/embeds only -> ignored with no side effects; attachment publication remains Stage 8/out of scope.
- **Duplicate event:** same message delivered sequentially and concurrently -> one canonical row/source and one gateway request where process-local serialization is available; if both reach the render endpoint, deterministic IDs plus DB uniqueness still yield one committed result and the loser resolves to `already_published` rather than erroring outward.
- **Restart replay:** rebuild runtime against the same DB, replay event -> no new gateway call or follower snapshot.
- **Binding race:** classify message, then unbind/move before publisher validation -> ignored; no old-channel publication.
- **Guild policy race:** disallow guild before execution -> ignored fail-closed.
- **Global ban:** banned owner -> rejected without federation; no community-ban lookup is required.
- **Gateway failure:** render request raises before response -> no local artifacts; replay can succeed with same deterministic IDs.
- **Commit failure after render:** injected SQLite failure -> no partial rows; replay sends identical IDs/JSON request and commits once.
- **Follower partial failure:** publication commit succeeds; healthy endpoint delivers, failed endpoint remains retryable; Discord callback does not roll back the post.
- **No eager thread:** assert Discord thread APIs and thread mapping repositories are untouched.
- **Mode isolation:** existing forum starter/reply events still route to community runtimes; announcement, forum, thread, DM, unbound text, and stale/corrupt binding channel types do not enter personal-feed publication.

### Persistence and migration tests

- Existing community rows become `publication_context='community'` with unchanged IDs, timestamps, object/activity URLs, mutation foreign keys, and outbound-source references.
- Personal-feed rows require null community URL; community rows require non-null URL; invalid combinations fail.
- Uniqueness remains enforced for Discord source, Discord message, activity ID, and object ID.
- Mapping conflict with the same Discord ID but another actor/context fails closed and is never overwritten.
- Follower snapshot/shared-inbox deduplication matches Stage 5 behavior.

### Gateway and protocol tests

- Personal-feed request produces `Create` containing `Note`, not `Page`, with no community `audience` or recipient.
- `to` contains Public and `cc` contains the User Actor/followers according to the chosen canonical shape.
- `id`, `url`, `attributedTo`, activity actor, source, HTML content, and published timestamp are present and stable.
- Operation/object/activity mismatch, wrong username path, foreign origin, malformed URL, or unsupported kind returns a bounded 4xx response without signing/delivery.
- Endpoint does not call `sendActivity` or signed-delivery helpers.
- Public dereference and Create reconstruction after restart match the originally returned canonical JSON semantically.
- Existing community publish builders retain current Page/comment Note addressing and delivery behavior.

### Validation commands

Use the repository’s exact current scripts discovered during implementation, including at minimum:

```text
pytest tests/behavior/test_personal_feed_stage7_root_publication.py
pytest tests/behavior/test_personal_feed_stage6_control_plane.py
pytest tests/behavior/test_publish_scenarios.py
pytest tests/behavior/test_user_actor_content_fanout_scenarios.py
pytest tests/test_migrations.py
pytest
python -m compileall src tests
ruff check src tests
ruff format --check src tests
mypy src
npm --prefix fedify-gateway test
npm --prefix fedify-gateway run typecheck
npm --prefix fedify-gateway run build
```

Also run any repository-specific clean-schema migration, Docker/config validation, and formatting commands defined by current project scripts or CI. A command absent from the repository must not be invented as a passing check; record the actual equivalent used.

## Regression and Blind-Spot Analysis

- **Duplicate public identities:** deterministic IDs and DB uniqueness must cover ambiguous gateway/commit failures, not only ordinary duplicate Discord callbacks.
- **Wrong author:** never infer ownership from guild permissions or channel ownership alone; compare exact Discord user ID against the persisted binding owner.
- **Stale binding leakage:** revalidate current guild/channel binding immediately before dispatch so moved/unbound channels cannot publish.
- **Community semantic drift:** nullable community fields must not remove community recipients, Announce envelopes, or mutation routing from existing modes.
- **Follower audience drift:** store the exact Create JSON before worker delivery; retries must not reconstruct against a later profile, follower set, or timestamp.
- **Content loss:** do not truncate at Mastodon’s local composition limit and do not treat empty Message Content caused by a missing privileged intent as a successful blank post.
- **XSS/log leakage:** rely on the established Markdown sanitizer and keep raw content out of logs.
- **Transaction duration:** no Discord or gateway network I/O inside `BEGIN IMMEDIATE`.
- **Reply leakage:** a message in a Discord thread remains under existing routing; Stage 7 must not interpret thread messages as personal-feed roots even when their parent is the bound channel.
- **Mutation leakage:** raw edit/delete handlers must continue to ignore personal-feed rows until Stage 8. The new context fields may make them discoverable, so tests must prove no accidental propagation occurs.
- **Outbox overclaim:** public object dereferencing is required now; user outbox pagination remains empty until Stage 9.

## Stage Boundary and Handoff

### Contracts changed by Stage 7

- Personal-feed owner root messages can create public User Actor `Create(Note)` activities.
- Canonical mapping/object persistence has an explicit community vs personal-feed context.
- Personal-feed creates use deterministic externally dereferenceable IDs.
- Gateway supports render-only personal-feed Create construction.
- Existing durable User Actor follower delivery accepts personal-feed content-create sources.

### Contracts intentionally preserved

- Registration, profile, follower lifecycle, Stage 4/5 retry semantics, community publishing, local-community Announce, bans outside this new path, and all current Discord forum routing remain unchanged.
- Existing community rows and public URLs survive migration unchanged.
- User Actor private keys remain confined to the gateway signing boundary.

### Deliberately left for later stages

- Stage 8: owner/non-owner replies, registration prompts in feed threads, manual/lazy thread mapping, inbound comments, edit/delete handling, stale-mapping synchronization suppression, and Discord delivery retries.
- Stage 9: populated User Actor outbox, pagination, retention/diagnostics, restore tooling, and final operational hardening.
- Media attachments, private/followers-only visibility, historical backfill, and reconstruction into replacement Discord channels remain out of scope.

### Assumptions handed to Stage 8

- Every personal-feed root has one durable `PublishedActivityObject` and `MessageMapping` with `publication_context='personal_feed'`, null community URL, stable actor/object/activity IDs, source channel/message IDs, and `in_reply_to_object_id=NULL`.
- The public object route can reconstruct the root independently of Discord.
- Binding movement/removal does not alter existing publication rows.
- No temporary adapter, fake community, duplicate publication API, placeholder state, or broken caller remains.

## Open Questions

None. The umbrella plan, Stage 6 contracts, current code, and verified external protocol behavior determine the required architecture without a coordinator product decision.
