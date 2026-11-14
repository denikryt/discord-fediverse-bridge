# Event flows

This document explains the major runtime traces through the bridge: user/platform action, important preconditions, modules called, database effects, and outbound effects. It owns step-by-step behavior flow descriptions, not exhaustive function internals.

## Registration flow

1. `/register` starts in `src/commands/register.py` and browser routes in `src/http_api.py`.
2. Discord OAuth state is stored through `src/registration_service.py`.
3. Successful completion creates a `users` row with stable local actor URLs and key material.

## Remote /subscribe-community flow

1. `src/commands/subscribe.py` uses `src/community_discovery.py` for Bridge,
   verified NodeBB `Group` (`src/nodebb_discovery.py`), or Lemmy discovery,
   checking policy before network and again for the resolved actor.
2. `src/operations/subscribe.py` receives canonical actor identity; a NodeBB
   category `cid` is never used as a Lemmy community ID.
3. The gateway `/prepare-community-follow` returns the verified canonical Group/inbox and an immutable Follow ID **without sending**.
4. Python inserts the `bridge_actor_follows` generation and channel binding together inside `BEGIN IMMEDIATE`; a CAS `prepared→sending` selects one sender, then `/send-prepared-community-follow` submits exactly the committed activity.
5. Network ambiguity retains `sending` and the original ID; it does not trigger a new Follow. Signed Accept must come from the saved Group actor, reference the original Follow ID, and match an uncancelled pending generation; its update of all matching channel bindings is atomic. Only verified threadiverse may accept implicitly on inbound content; NodeBB requires explicit Accept.

## Remote /unsubscribe-channel flow

1. `src/commands/unsubscribe.py` calls `src/operations/unsubscribe.py`.
2. Under the same SQLite writer boundary as Follow send reservation, remove the
   channel binding and inspect the existing Follow generation. Another active
   channel keeps the original Group Follow unchanged.
3. When the last channel is removed, `prepared` Follow is cancelled **before**
   network send; `sending` or `sent` Follow records `cancel_requested` without
   inventing a replacement Follow ID. Only a confirmed sent Follow may advance
   to `undo_sending` before the gateway sends the deterministic Undo ID.
4. Uncertain Follow/Undo transport does not prove remote cleanup or delete the
   saved generation. Resubscribe before Undo starts can rebind the **same**
   Follow; after `undo_sending`, it requires proven cleanup or reports
   `remote_follow_cleanup_unresolved` with the guarded offline operator path.

## Local Group inbound post/reply host flow

1. The gateway resolves every authenticated local Group recipient separately
   (actor-specific owner or bounded shared inbox); the Python receipt is scoped
   by wire ID, event type and verified Group actor.
2. For each tenant, `LocalCommunityInboundCreateHandler` checks its accepted
   direct actor or cryptographically verified Bridge Person→Service relationship
   **before** any existing-object or parent lookup.
3. `LocalCommunityContentRepository` reserves one immutable `(Group,object,kind)`
   with original owner/Service origin, the first rendered Discord Create body
   and a fencing attempt ID under `BEGIN IMMEDIATE`. Reply roots and direct
   parents must belong to this Group. Later signed Create/Announce duplicates
   use the saved title/body when completing a missing sibling; their new
   payload cannot replace what the original host received.
   Another concurrent Create/Announce may observe the row but cannot issue a
   second host Discord REST call.
4. A successful REST response atomically attaches the corresponding host
   surface and marks `delivered`; verified sibling surfaces and relay follow.
   A pre-send error marks `failed`; a possible send with lost response marks
   `unknown` and is not blindly retried. A duplicate of an unknown attempt
   cannot generate another host or relay send.
5. The global AP `MessageMapping` is optional compatibility provenance, not
   proof of a Group host. A gateway parent fallback with more than one local
   host placement returns an explicit ambiguity instead of selecting one.
   Existing pre-v1 host rows without a provable initial content snapshot do
   not gain new sibling content from a later envelope; that replay fails closed.

## Local Group canonical Create relay flow

1. The completed tenant-scoped host Create supplies the immutable original
   Person owner and origin subscriber (a Service for Bridge Person publication).
2. `LocalCommunityFederationFanout` excludes that original Service and selects
   currently accepted, policy-allowed target actors/inboxes. Repeated Announce
   wrappers reuse the same source identified by `(tenant, AP object, Create)`.
3. `LocalCommunityRelayRepository` stores the first raw source and, only when
   needed, its first verified normalized projection; later source JSON and
   author changes cannot replace it. It reserves each target under SQLite
   `BEGIN IMMEDIATE`, then claims `pending|failed→sending` with a fencing token
   and stable relay activity ID before gateway HTTP.
4. The existing Fedify gateway signs/sends this exact JSON. A matching verified
   result yields `delivered`. Only an explicit pre-send rejection allows
   `failed` retry with the original identity; unknown or mismatched HTTP result
   stays `unknown` without blind retry. Late completion cannot modify an
   attempt claimed under another token.
5. An independently verified Update/Delete receives its own revision source and
   may target only accepted actors with a confirmed original Create delivery.
   The mutation owner, original Service and Group stay unchanged.

## Local Group inbound Update/Delete flow

1. The gateway forwards the authenticated outer `Update` or `Delete` sender
   and exact verified local Group recipient; a Group-signed nested mutation
   alone does not supply a Person signature. Python admits a separate Group
   receipt only after this recipient boundary.
2. `LocalCommunityMutationHandler` confirms the currently accepted original
   same-actor subscriber or independently verified Bridge Person→Service
   relationship. The signed activity's object identity, author and explicit
   `inReplyTo` must agree with normalized source fields.
3. `LocalCommunityContentRepository` verifies, under a SQLite writer lock,
   `(local_community_id, ap_object_id, kind, source_owner_actor_id,
   origin_subscriber_actor_id)` and the original reply root/parent chain.
   Unknown historical owners, a revoked subscription, an undelivered host,
   or foreign Group/parent fails before any Discord or relay effect.
4. Only that exact canonical row is passed to Discord surface fanout; signed
   ID-only `Delete` is permitted for the known original owner, without
   interpreting a fresh Tombstone as new ownership evidence. Later relay
   follows only after this authorization succeeds.

## Local Group inbound Follow/Undo(Follow) flow

1. Fedify verifies the signed Follow/Undo and routes the verified local Group
   target to the authenticated Python event endpoint. A bare matching actor or
   an ID-only cached raw payload does not establish authority.
2. `src/http_api.py` claims the wire receipt. Failed/deferred Follow receipts
   preserve an internal retry bit before reset; no external retry bit is trusted.
3. `LocalCommunityFollowerHandler` checks policy and inbox admission, then
   `RemoteSubscriberRepository.create_or_refresh_accepted()` serializes the
   subscriber generation under `BEGIN IMMEDIATE`. A retry of the current ID
   may repeat Accept; a superseded/removed generation is terminal skipped and
   cannot publish, refresh its inbox or send Accept.
4. For verified `Undo(Follow)`, the repository deletes only a row with the exact
   non-empty current Follow ID and accepted status in one SQL statement. A late
   Undo(old) leaves Follow(new) intact even during concurrent refresh. No
   separate read is used to authorize deletion.

## Discord thread/reply -> remote Group Create flow

1. `src/discord_bot.py` receives a Discord event; the router chooses the accepted remote subscription and `CommunityRuntime` checks its forum/parent context.
2. `ContentPublishService` requires the persisted verified `BridgeActorFollow.remote_software_profile` (`nodebb`, `threadiverse`, or `bridge`), the accepted channel binding, original Follow ID and original Group inbox. `unknown` cannot fall back to Page.
3. An immutable operation digest from Discord guild, forum, message, registered Person, Group and kind produces typed AP object/Create IDs. NodeBB roots use Note/context; known Lemmy/Bridge roots preserve Page; all replies use Note with canonical parent. Public object GET and User Actor outbox reconstruct the original object's type and context from the saved immutable Create JSON (not from current software discovery); legacy rows without a source preserve the older renderer fallback.
4. Gateway `/render-community-create` validates and renders the caller-supplied IDs **without** ActivityPub sends. Python verifies the returned Create, then atomically persists mapping, original object, source JSON, follower audience and direct Group recipient snapshot under one SQLite writer transaction. Exact duplicates reuse the same source.
5. After commit, `queued/locally_accepted` means the original Create exists in the outbox, **not** that NodeBB accepted it. `CommunityRuntime` records one canonical thread/reply group and source Discord delivery, then mirrors to accepted sibling forums even while remote transport remains queued. `BridgeBot.on_thread_create` remembers the locally accepted thread for reconnect deduplication. Terminal local rejection does not create groups or mirrors.
6. The existing `UserActorDeliveryWorker` claims the source's original direct Group endpoint even when the author has zero followers. It rechecks the exact accepted Follow generation, channel binding and Group/inbox policy; matching accepted User Actor followers share the same endpoint request, but cannot resurrect a revoked Group grant. An ambiguous direct transport result is retained as `unknown` without blind retry. Follower-only delivery retains its existing retry policy.

## Discord Update/Delete -> original remote Group

1. A Discord edit/delete of a published root or reply records a desired
   mutation on the original `PublishedActivityObject` and reserves a stable
   activity ID under a SQLite writer transaction. The reservation copies the
   original Create Group/inbox/Follow/software evidence; missing evidence for a
   remote Group is an explicit refusal, never a fresh actor discovery.
2. `ContentMutationRepository.claim_due` waits until the original direct
   Create delivery is confirmed `delivered`. An unknown network result does
   not grant permission to render or publish a mutation.
3. The gateway renders the deterministic Update/Delete without sending. Python
   verifies its canonical identity/owner and atomically commits a new source
   with the same direct recipient and endpoint, even across worker restarts.
4. The existing User Actor worker checks the current exact Follow generation,
   forum binding and policy. It delivers Create then revisions in order:
   neither Update nor terminal Delete may bypass an unresolved earlier direct
   send; an unknown outcome is held rather than retried blindly.

## Inbound receipt observability

`src/http_api.py` creates an `in_progress` receipt before dispatch. Terminal handlers return a lifecycle `status`, a stable `InboundActivityOutcome`, and human-readable `detail`; `EventReceiptRepository` persists all three together. Deferred or failed retries reset the row to `in_progress` and clear the previous outcome. Duplicate terminal deliveries return the stored outcome without rewriting the receipt.

## Verified local Group inbox -> scoped receipt flow (CP19)

1. Fedify verifies the incoming activity. Its typed, verified JSON is compared
   with the server-owned actor-specific inbox owner, or with a bounded set of
   real local Group actors in a shared inbox; the first raw `to`/`cc` address
   is not a routing authority. Missing owner provenance or contradictory local
   recipients are rejected before Python delivery.
2. A signed Create addressed to Group A and B produces two internal events,
   both retaining the original wire `delivery_id`. Each carries only its own
   `verified_destination_group_actor_id`, and an individual failure does not
   prevent the other tenant's admission.
3. Python authenticates bearer/header, checks the verified destination against
   its local Group row, then claims/finishes a receipt keyed by wire ID, event
   type and Group. Earlier NULL-scope receipts and remote Group/Follow/personal
   receipts remain global. Independent Service/Person authorization and host
   reservation must still follow (CP20–CP21).

## Remote ActivityPub post -> Discord fanout flow

1. Fedify emits a verified remote Group `post.created`; Python checks the accepted subscription and event author before any Discord effect.
2. `CommunityRuntime.handle_inbound_post` atomically reserves `(community_actor_id, ap_object_id)` with an immutable original owner **and first verified Create object JSON** in `CommunityThreadGroup`, before Discord REST. A different Group can store the same object ID independently. A later Announce cannot replace the first title/body when completing another forum. Legacy roots without original owner or first Create snapshot cannot acquire new destinations.
3. For each still-accepted Discord forum, `DiscordFanoutGroupRepository.reserve_remote_target` atomically claims the typed target before REST. Existing confirmed delivery or `claimed/unknown/completed` attempt cannot send again; an explicit pre-send failure may retry under a new attempt token.
4. On confirmed Discord success, the existing `CommunityThreadGroupDelivery` and completed claim commit together. Failure while fetching the forum or awaiting bot readiness **before** Discord REST is a provably unsent `failed` claim; an exception after starting `create_thread` is `unknown` and is not blindly resent. A subsequent valid AP event, **including a retry with the same wire `Announce.id` after a terminal receipt**, re-enters this Group-scoped target-claim boundary and can complete only safe missing targets; its original terminal receipt stays unchanged. No restart sweeper runs.

## Remote ActivityPub comment -> Discord fanout flow

1. The reply is scoped to the accepted Group's existing canonical root; its parent, if any, must belong to the same Group and root. A different tenant's root or reply cannot supply a Discord target.
2. `CommunityMessageGroup` reserves the immutable `(Group, object, owner, root, parent)` and first verified reply object JSON before Discord. A later Announce cannot replace the first reply body. Each confirmed root delivery is a candidate reply target only while that channel remains subscribed.
3. The same typed target claim rules apply to `_send_inbound_comment`; the confirmed `CommunityMessageGroupDelivery` and claim are committed atomically. Errors in bot readiness or parent-reference preparation before `send` are retryable `failed`; an ambiguous Discord `send` remains `unknown` without automatic retry. Same-wire authenticated repeats may revisit only the per-forum claim boundary, not skip it because of a terminal receipt.
4. A missing historical root/target is deferred unless there is verified Group-distribution or bounded official category-outbox membership evidence. NodeBB-specific verification and historical root reconstruction are owned by CP13; a fetched object's `context` or `audience` alone never authorizes placement.

## Remote Group Update/Delete -> existing Discord surfaces

1. Fedify checks the inbound signature and gateway normalizes the original
   `Update`/`Delete` activity. Its `source_activity_json` is the verified typed
   event, **not** an ID-only raw-cache entry. Nested Group `Announce(Update|Delete)`
   carries only a Group signature; it is not evidence of a Person signature.
2. `CommunityRuntime` selects one canonical post or reply by **Group + AP object
   ID + kind** and requires an immutable non-NULL `source_owner_actor_id`, a
   signed direct activity from exactly that original owner, matching original
   object ID and Group addressing, and an active Group subscription.
3. Reply mutations also match the saved root/parent. An ambiguous canonical
   root/reply identity, foreign Group/actor, changed parent, unknown historical
   owner or Group-only mutation is rejected **before** reading delivery targets
   or performing Discord REST. No moderator delegation is inferred from Group
   signatures or `Tombstone.attributedTo`.
4. NodeBB roots and replies may both use `/post/{pid}`. On Delete, if the
   gateway's URI-based kind hint does not match the DB canonical kind, Python
   resolves the proven kind before receipt admission and owner checks; it
   never picks an unrelated Group's mapping. Direct signed owner Update/Delete
   edits only existing bot-owned Discord deliveries.

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
4. For inbound remote update/delete, the runtime first verifies the signed original owner, accepted origin subscriber, exact tenant, kind and parent under the same SQLite writer lock. Only authorized actions mutate the canonical row's host and sibling surfaces, then use the existing remote relay eligibility rule.
5. Inactive local-subscriber surfaces are historical copies, not authoritative mutation sources.

## Remote ActivityPub Undo(Follow) -> local community unfollow flow

1. Gateway verifies the signed `Undo(Follow)` and the exact verified local
   Group recipient; an ID-only raw cache cannot supply the embedded Follow.
2. Gateway emits `local.unfollow_requested` for that owner; Python validates
   the authenticated actor and original Follow identity.
3. Under SQLite writer serialization, delete from `remote_subscribers` only
   where `(local_community_id,remote_actor_id,follow_activity_id,status)`
   matches the **current** accepted generation and Follow ID is non-NULL.
   Late `Undo(old)` after `Follow(new)` and a missing Follow ID do no deletion.
4. Subsequent Create/Update/Delete and relay reevaluate current accepted
   subscribers; a retry of old Follow cannot restore an undone generation.


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

## Verified NodeBB Group Announce -> Discord root/reply

1. A signed Fedify `Announce` on `/inbox` carries the original outer ID and
   verified distributor. The NodeBB route does **not** require a local Group
   recipient and does not use the ID-only raw cache.
2. Gateway passes a `remote.group_announced` event with the original inline
   Note/Article or referenced URI. Python admits only the same persisted,
   accepted `nodebb` Group/inbox, checking federation policy **before** fetch.
3. `src/community_sync/nodebb_inbound.py` safely dereferences the exact Note,
   checks canonical object ID, audience, recipients, author and topic context.
   A root Note is a post; a Note with `inReplyTo` is a reply, whose parent chain
   is bounded. The Group signature proves distribution, not a Person signature.
   For Discord attribution, Python optionally fetches the exact `attributedTo`
   Person actor via the bounded policy-checked HTTPS adapter and uses a validated
   `preferredUsername`. Failure leaves a last-path-segment display fallback;
   neither the fetched label nor that fallback changes canonical owner identity.
4. For a root created before Follow, an official Group outbox item must
   announce its exact ID; no match within the bounded view yields a deferred
   result, not an inferred membership from `context` or `audience`.
5. Existing `CommunityRuntime` reserves canonical Group/owner and Discord
   targets before REST. Unknown Discord outcomes remain unresolved.
   New Discord forum starters and replies suppress automatic link embeds, as
   do sibling/local-community mirrors and personal-feed replies. This does
   not edit existing Discord messages or retroactively repair saved names.
