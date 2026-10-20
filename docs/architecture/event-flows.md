# Event flows

This document explains the major runtime traces through the bridge: user/platform action, important preconditions, modules called, database effects, and outbound effects. It owns step-by-step behavior flow descriptions, not exhaustive function internals.

## Registration flow

1. `/register` starts in `src/commands/register.py` and browser routes in `src/http_api.py`.
2. Discord OAuth state is stored through `src/registration_service.py`.
3. Successful completion creates a `users` row with stable local actor URLs and key material.

## Remote /subscribe-community flow

1. `src/commands/subscribe.py` calls `src/operations/subscribe.py`.
2. `src/lemmy_client.py` resolves the remote community when needed.
3. `src/db/database.py` records `channel_community_subscriptions` and bridge follow state.
4. `src/fedify_gateway_client.py` calls gateway `/follow-community`; gateway sends the Follow.
5. Accept returns through gateway `/inbox`, `src/http_api.py`, and `src/activitypub_handlers.py`.

## Remote /unsubscribe-channel flow

1. `src/commands/unsubscribe.py` calls `src/operations/unsubscribe.py`.
2. The channel subscription is removed.
3. If it was the last channel for the community, Python calls gateway `/unfollow-community`.
4. The bridge follow row is removed or preserved according to Undo(Follow) delivery outcome.

## Discord thread -> remote Lemmy community publish flow

1. `src/discord_bot.py` receives a Discord event.
2. `src/discord_event_router.py` selects remote subscription runtime.
3. `src/community_sync/runtime.py` validates context.
4. `src/content_sync/outbound_publish.py` builds the publish request.
5. Gateway `/publish` signs and sends Create; Python persists object/mapping state.

## Inbound receipt observability

`src/http_api.py` creates an `in_progress` receipt before dispatch. Terminal handlers return a lifecycle `status`, a stable `InboundActivityOutcome`, and human-readable `detail`; `EventReceiptRepository` persists all three together. Deferred or failed retries reset the row to `in_progress` and clear the previous outcome. Duplicate terminal deliveries return the stored outcome without rewriting the receipt.

## Remote ActivityPub post -> Discord fanout flow

1. Gateway `/inbox` receives and normalizes ActivityPub to `post.created`.
2. `src/activitypub_handlers.py` validates idempotency and routes to remote subscription runtime.
3. If no accepted subscription and no existing mapped thread context exists, Python skips the event locally while gateway delivery remains acknowledged.
4. Otherwise Discord threads are created and group/delivery rows are recorded.

## Remote ActivityPub comment -> Discord fanout flow

1. Gateway normalizes inbound comment ActivityPub to `comment.created`.
2. Python validates the event and checks accepted subscription or already mapped parent/post context.
3. Unsubscribed comments without mapped context are skipped before backfill or Discord fanout.
4. Runtime creates Discord messages in mapped thread deliveries and records message group rows.

## Discord edit/delete -> ActivityPub update/delete flow

1. Discord edit/delete enters `src/discord_bot.py`, which delegates raw admission, echo suppression, and partial edit reconciliation to `src/discord_message_mutations.py`.
2. `src/discord_message_mutations.py` routes admitted mutations through `src/discord_event_router.py` with the startup-owned `DiscordMutationContext`; the raw path does not pull a broad `Runtime` out of the bot.
3. The selected runtime resolves persisted ActivityPub object ids through `src/db/database.py` and uses the context's narrow delivery wake/gateway capabilities for outbound Update/Delete.
4. `src/content_sync/edit_delete.py` prepares the request.
5. Gateway `/update` or `/delete` signs and sends the ActivityPub operation.

## /create_community flow

1. `src/commands/create_community.py` calls `src/operations/create_community.py`.
2. `src/local_communities/service.py` creates stable local community actor metadata.
3. Python stores `local_communities`; gateway later serves the actor from DB.

## Discord thread -> local ActivityPub community publish flow

1. Router selects `src/local_communities/runtime.py` for the host forum or an active local subscriber forum of a local community.
2. Python records one canonical local-community thread/message row and a source Discord surface. Host sources create a `role="host"` source surface; local subscriber sources create a `role="local_subscriber"` source surface.
3. Python calls gateway `/publish-local-community`; gateway creates local ActivityPub content and delivers it to accepted remote subscribers.
4. Local Discord fanout creates missing target surfaces: host sources copy to local subscribers, while local subscriber sources copy to the host forum and sibling local subscribers.

## Remote ActivityPub Follow -> local community remote-subscriber flow

1. Gateway receives Follow to a local community actor and emits `local.follow_requested`.
2. Python stores `remote_subscribers` and asks gateway to send Accept(Follow).

## Accept local community follow flow

1. Local runtime accepts a remote follow.
2. Python calls gateway `/accept-local-community-follow`.
3. Gateway signs Accept(Follow) as the local community actor.

## Remote ActivityPub content -> local community Discord/federation relay flow

1. Gateway normalizes content addressed to a local community.
2. Python routes by `community_actor_id` to local community runtime.
3. Runtime mirrors into the host Discord forum and records canonical rows plus host surfaces.
4. Local Discord fanout creates missing local subscriber surfaces for active same-instance subscriber forums.
5. Gateway `/send-local-community-relay` signs and delivers rendered relay activities to other accepted remote subscribers.

## Local community edit/delete flow

1. Discord edit/delete enters through `src/discord_message_mutations.py`, then `src/discord_event_router.py` resolves the starter/reply surface row for the raw message id.
2. `src/local_communities/runtime.py` resolves the canonical post/comment and loads the `PublishedActivityObject` by canonical AP object id, not by the copied Discord message id.
3. For host or active local-subscriber source surfaces, Python sends one gateway Update/Delete and `src/local_communities/discord_fanout.py` mutates every other persisted local Discord surface.
4. For inbound remote update/delete, the runtime mutates every persisted local Discord surface, including host and local subscribers, then keeps the existing remote relay eligibility rule for remote subscribers.
5. Inactive local-subscriber surfaces are historical copies, not authoritative mutation sources.

## Remote ActivityPub Undo(Follow) -> local community unfollow flow

1. Gateway receives embedded Undo(Follow) addressed to a local community actor.
2. Gateway emits `local.unfollow_requested` when the target actor is owned by this bridge.
3. Python removes the remote actor from `remote_subscribers`.
4. Future create, update, and delete fanout uses current accepted remote subscribers and excludes the removed actor.


## Local subscriber Discord post/comment -> local community flow

1. `src/discord_event_router.py` resolves the source forum through `src/local_communities/participant_routing.py`.
2. Active `LocalSubscriber` forums route into `LocalCommunityRuntime`; inactive subscribers and unrelated forums do not.
3. Runtime publishes the source Discord starter/reply through the existing local-community publish path and persists one canonical row.
4. Runtime records the source `role="local_subscriber"` surface and then fans out to the host forum plus sibling active local subscribers.
5. Stage 5 handles later edits/deletes from the source local-subscriber surface through the participant-wide mutation flow above.


## Disabled local community lifecycle flow

1. `/edit-community` opens a Discord modal for owner/super-admin callers and can save `status=active` or `status=disabled` alongside display metadata.
2. `src/local_community_lifecycle.py` is the shared lifecycle decision point; disabled communities keep existing DB state but reject new side effects.
3. Inbound ActivityPub deliveries targeting a disabled local community are acknowledged and routed through the existing skipped result path before domain mutation, fanout, subscriber creation, or outbound federation.
4. Discord-originated post, comment, edit, delete, and local subscription paths resolve the local community and stop before publish, persistence, relay, or Discord fanout when status is disabled.
5. Existing actor/object routes, content rows, subscribers, and Discord surfaces remain readable and unchanged; re-enabling returns existing subscriber rows to normal fanout behavior.

## Personal-feed binding and Stage 6 routing flow

1. `/account-settings feed-bind` resolves the invoking registered user, checks the existing guild policy, requires Discord Manage Server permission, and accepts protocol `GUILD_TEXT` and `GUILD_ANNOUNCEMENT` channels.
2. The command snapshots the caller's current binding and submits one cancellation-safe synchronous mutation to `PersonalFeedService`.
3. The service acquires SQLite's writer reservation, rechecks user/guild/channel uniqueness and cross-mode channel ownership, then inserts or compare-and-set moves the single binding.
4. `/account-settings feed-view` reads the durable assignment; `feed-unbind` removes only the caller's current assignment. Historical publication/thread/message mappings are not modified.
5. `BridgeBot.on_message` recognizes owner-authored root messages in an active ordinary text-channel or announcement-channel binding before existing forum routing. Non-owner roots, stale bindings, threads, DMs, and unrelated channels do not classify as personal-feed roots.

## Stage 03 normalized Discord Gateway event boundary (in progress)

`src/discord_gateway_events.py` converts observed SDK callbacks into the frozen
`ThreadCreated`/`ThreadUpdated`/`ThreadDeleted` and `MessageCreated`/`MessageUpdated`/
`MessageDeleted` platform types. It keeps the immediate message container and
source timestamp. An absent raw edit body or missing starter evidence stays
unknown; raw delete never needs cache access. Proven bot/webhook identities and
guild/channel metadata are checked before any domain publication. The live
`BridgeBot` to `PlatformEventIngress` cutover remains pending, so these DTO
conversions are not yet the deployed callback path.

## Stage 03 Personal Feed inbound Discord delivery safety

1. An authorized remote Create, Update or Delete mutates the canonical inbound conversation object under `BEGIN IMMEDIATE`; each due claim captures the exact revision fingerprint and deleted flag before physical Discord work.
2. The Personal Feed worker rechecks the active binding. Before the first unknown-ID message send, it durably marks `CREATE_STARTED_DISCORD_SENTINEL`; the network call runs outside the SQLite transaction.
3. An interrupted or ambiguous first Create is parked with `UNCERTAIN_DISCORD_CREATE_ERROR`, not retried blindly. Later ActivityPub Update/Delete changes the canonical object but cannot clear this marker or trigger another Create.
4. On a verified Discord receipt, `record_discord_attempt` saves the physical ID and compares the completed claim to the current canonical revision. A superseded edit/create schedules the latest **edit/delete using that existing ID**; it cannot acknowledge an obsolete revision as delivered.
5. A known-ID edit/delete failure uses the bounded three-attempt policy only for its claimed revision. Restart recovery preserves parked unknown first Creates but safely reclaims unrelated interrupted work.

## Versioned startup and channel claims

Startup recognizes released v0.1.0 schema at version 0 and validates the complete current schema at version 1. Empty files bootstrap current v1 atomically. The sole registered v0→v1 step reserves a dedicated connection, suspends FK enforcement before `BEGIN IMMEDIATE`, and rechecks header and inventory under the writer lock. All 30 released tables are staged in SQL TEMP before any DROP; the 45-table current ORM target is created once. Native INSERT/SELECT and JOIN build scoped locations and typed claims, followed by the planned one-time AP identity deltas. Published objects retain their source IDs; features absent from released v0 stay empty. No Stage 00 schema or internal version is created. Target definitions, every original row, `foreign_key_check` and `integrity_check` are verified before the sole version-1 stamp and commit. Errors restore original schema/data and header; FK enforcement is restored and verified in `finally`, with unsafe connections invalidated. Current v1 startup is read-only and never repairs a partial schema.

Subscribe resolves policy and performs gateway work outside the writer transaction. Persistence rechecks the typed channel owner and updates only the expected failed subscription in place; replacement or concurrent claims fail closed. Shared ActivityPub Follow state is global. Discord fanout selects accepted/active current primary claims, while history lookups use scoped copy locations independently of current ownership. Personal-feed move releases the old claim and claims the new location in one writer transaction.

During released identity cutover only relationships of renamed actors lose confirmation: changed communities lose incoming remote_subscribers; changed service/local target relations keep their Follow/channel rows as failed with cleared Follow IDs. Local subscribers and claims survive. Normal subscribe retry sends a new Follow after commit; migration performs no delivery itself.
