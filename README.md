# Discord–Fediverse Bridge

A self-hosted bridge between Discord forum channels and ActivityPub community actors. The primary interoperability target is Lemmy and the wider Threadiverse. The project also implements a limited Mastodon-compatible subset for replies to locally hosted communities.

This is not a generic ActivityPub server, a Discord archive, or a tool for exporting an entire Discord server. It synchronizes new activity only through explicitly mapped Discord forum channels. One bridge deployment can serve multiple Discord guilds.

## Supported operating modes

### Remote community subscription

A Discord forum channel can be assigned to a remote ActivityPub community, such as a Lemmy community.

- The bridge's shared `Service` actor sends one `Follow` per remote community.
- Multiple Discord forum channels, including channels in different Discord guilds served by the same bridge deployment, can subscribe to the same remote community and reuse the same accepted Follow.
- Each supported inbound event from that remote community is fanned out to every subscribed Discord forum channel that is eligible under current bridge policy. A remote post therefore creates a corresponding thread in each subscribed forum, and a remote comment creates a message in each corresponding Discord thread.
- New Discord threads and messages are published back to the remote community when their authors are registered with the bridge.
- Supported edits and deletes are propagated in both directions.
- When the last Discord channel unsubscribes, the bridge sends `Undo(Follow)`.

The Follow belongs to the shared bridge actor. Individual Discord user actors are used for content authorship, not for following remote communities.

### Local community hosting

A Discord forum channel can be exposed as a local ActivityPub `Group` actor.

- The selected forum is the host surface for the community.
- Other Discord forum channels on the same deployment, including channels in other Discord guilds, can subscribe as local subscriber surfaces. This is an internal bridge subscription and does not use ActivityPub between those Discord guilds.
- Remote ActivityPub actors can follow the community.
- The host forum, all active local subscriber forums, and accepted remote followers participate in the same community fan-out. A supported post, comment, edit, or delete originating on any participating Discord surface is propagated to the other local Discord surfaces and published to eligible remote followers.
- A supported inbound ActivityPub event for the local community is mirrored to the host forum and active local subscriber forums, and is relayed to the other eligible accepted remote followers with loop suppression and delivery tracking.
- A community can be changed between `active` and `disabled`. Disabled communities remain manageable but reject new publishing, following, subscription, and moderation activity until re-enabled.

The project does not currently implement permanent removal of a local community actor.

## Explicit routing boundaries

The bridge does not scan or federate every channel in a Discord server.

Content is handled only when all applicable conditions are true:

1. The Discord guild is allowed by bridge policy.
2. A specific Discord **forum channel** has been assigned to a remote subscription or local community.
3. The event occurs in a thread belonging to that mapped forum channel.
4. The Discord author is not a bot.
5. For Discord-to-Fediverse publishing, the author has registered a local bridge identity.
6. The applicable community, federation, and ban policies allow the action.

There is no historical backfill. Mapping a channel affects new supported activity; the bridge does not enumerate and republish its existing thread history.

Discord channel visibility is not a privacy boundary for ActivityPub. The project does not determine whether a selected forum is socially "public" or "private" inside Discord. Once a forum is mapped, eligible new content can be delivered to independent Fediverse servers. Operators should therefore use dedicated forum channels whose purpose and federation status are clear to participants.

## Discord user registration and consent boundary

Discord-to-Fediverse publishing is restricted to registered users.

Registration works as follows:

1. The user runs `/register` in an allowed Discord guild.
2. The bridge opens its web registration page.
3. Discord OAuth2 is used with the `identify` scope only.
4. The user chooses a local ActivityPub username.
5. The bridge creates a stable local `Person` actor and signing keypair for that user.

The OAuth flow reads the authenticated Discord account ID, username, and avatar URL. It does not request guild membership, email, contacts, or message history through OAuth.

If an unregistered user posts in a mapped forum thread, the bridge does not publish that message to ActivityPub and replies in Discord with a registration notice.

Registration is the current individual publishing opt-in. The project does not currently provide a separate per-message approval flow or a registered-user opt-out switch for individual mapped channels. A registered user who posts in a mapped forum is treated as participating in that forum's federation.

## Identity and attribution

Registered Discord users publish through their own local ActivityPub `Person` actors.

- ActivityPub objects are attributed to the registered bridge user actor.
- Discord display names are included in rendered content and mirror headers where required by the bridge mode.
- The local ActivityPub username is stable even if the Discord display name changes.
- The community actor does not impersonate the author of Discord-originated content.

Remote-community subscriptions are different: the shared bridge `Service` actor owns the Follow relationship, while the registered user actor owns each published post or comment.

## What is synchronized

Supported Discord-to-Fediverse flows:

- forum thread starter → post-compatible ActivityPub object;
- thread message → comment-compatible ActivityPub object;
- supported message/thread edit → `Update`;
- supported message/thread deletion → `Delete`.

Supported Fediverse-to-Discord flows:

- supported post create → a Discord forum thread in every eligible subscribed or local-community Discord surface;
- supported comment create → a Discord message in every corresponding eligible Discord thread;
- supported update → edits of the corresponding Discord mirrors;
- supported delete → deletion of the corresponding Discord mirrors;
- remote Follow/Undo for locally hosted communities;
- remote Accept for bridge-actor subscriptions.

The exact federation profile is documented in [docs/FEDERATION.md](docs/FEDERATION.md).

Not currently supported includes:

- vote synchronization;
- ActivityPub `Like`, `Block`, `Flag`, polls, reactions, or generic activity handling;
- ActivityPub Client-to-Server access;
- historical channel backfill;
- permanent local-community deletion;
- complete Mastodon server compatibility;
- forwarding ActivityPub abuse reports into Discord;
- guaranteed deletion from third-party servers;
- Discord attachment federation as a complete media pipeline.

## Edits, deletion, and data persistence

When a supported Discord-originated object is edited or deleted, the bridge sends the corresponding ActivityPub update or delete operation and updates its local mappings. When a supported remote object is edited or deleted, the bridge updates or removes its Discord mirrors.

ActivityPub delivery is distributed. A `Delete` requests deletion from remote recipients, but this service cannot guarantee that every independent server, cache, log, backup, or third party removes all copies.

The bridge database stores the state needed to operate and retry synchronization, including:

- registered Discord-to-ActivityPub identities and actor keypairs;
- community, subscription, follower, and policy state;
- Discord ↔ ActivityPub object mappings;
- delivery, retry, deduplication, and audit state;
- normalized content/object fields required by supported synchronization flows.

Deployment operators are responsible for database access, backups, retention, privacy policy, abuse handling, and legal compliance. This repository does not provide a hosted-service privacy policy.

## Moderation and policy

The bridge has deployment-local controls; it does not replace moderation on Discord or on remote Fediverse instances.

Supported controls include:

- Discord guild allowlists and blocklists;
- federation host allowlists and blocklists;
- bridge super-admins;
- community-scoped bans managed by the local community owner or a super-admin;
- bridge-wide global bans managed by a super-admin;
- disabling and re-enabling a local community;
- audit records for supported management actions and forbidden attempts.

A community-scoped ban applies to that local community across its host forum, local subscriber forums, and supported inbound/outbound bridge paths. A global ban applies across the deployment.

Current limitations include:

- bridge-local bans do not remove existing remote follower rows;
- bans do not emit federated `Block` or other moderation activities;
- ActivityPub `Flag` forwarding is not implemented;
- remote handle resolution for moderation is limited compared with full actor discovery;
- remote servers retain their own independent moderation and storage behavior.

See [notes/known_issues.md](notes/known_issues.md) for verified limitations and open defects.

## Discord access and permissions

The bot enables Discord's guild, message, and privileged message-content intents because it must receive the text of new messages in mapped forum threads.

Its runtime reads message content in these cases:

- new thread starters and messages inside mapped forum channels;
- raw edit payloads for mapped Discord-originated messages;
- the starter message of a newly created mapped forum thread;
- stored mirror-message IDs when applying supported edits or deletes.

`Read Message History` is used for the starter-message fallback and for fetching known messages by stored IDs. The implementation does not enumerate arbitrary server history, search unrelated channels, or backfill old discussions. However, Discord permissions are capability-based: if the bot is granted access to additional channels and message history, Discord may technically allow it to read messages there even though this code does not use that access for bridge routing. Operators should grant channel visibility and permissions only where required.

Typical capabilities used by the bridge include:

- view mapped forum channels and threads;
- receive message content;
- read message history for mapped threads;
- send messages and create threads;
- edit or delete bridge-created mirror messages;
- optionally create forum channels when a command omits the target channel, which requires `Manage Channels`.

The bridge does not use Discord OAuth access tokens to read messages. OAuth registration uses only the `identify` scope; message processing is performed by the installed bot under its guild/channel permissions.

## Command access

Most operational commands require:

- execution inside an allowed Discord guild;
- a caller who is not globally banned;
- bridge registration for commands that create communities, subscribe channels, or otherwise create federated state.

Creating a local community or subscribing a forum channel is currently available to any registered user in an allowed guild, subject to Discord application-command permissions and bridge policy. It is not intrinsically restricted to members with Discord `Manage Server` permission.

The creator becomes the local community owner. Community management is then restricted to that owner and bridge super-admins. Bridge-wide policy and global-ban operations are restricted to bridge super-admins.

Registered commands include:

- `/register`
- `/subscribe-community`
- `/unsubscribe-channel`
- `/list-subscriptions`
- `/create_community`
- `/edit-community`
- `/ban-user`
- `/unban-user`
- `/list-banned-users`
- `/publish-guild-invite`
- `/remove-guild-invite`
- `/bridge-policy add|remove|list`

## Public discovery and dashboard

Public actor discovery includes:

- `acct:username@bridge-host` for registered user actors;
- `acct:!slug@bridge-host` and supported equivalent community forms for local community actors;
- `GET /.well-known/discord-fediverse-bridge/communities` for bridge-specific community discovery.

The root path `/` serves a public dashboard. `/dashboard` redirects to `/`, and `/dashboard/data` exposes its JSON data.

The dashboard intentionally omits raw Discord guild/channel IDs, private keys, shared secrets, database paths, and internal service URLs. It publishes routing-oriented information such as local community names, last-known Discord guild/forum labels, accepted remote subscriptions, and active local subscriber surfaces. Disabled communities are currently included but are not specially marked in the dashboard UI.

## Process architecture

The deployment contains two cooperating processes:

1. **Python bridge**
   - Discord bot and slash commands;
   - registration web flow;
   - orchestration and policy;
   - database state, mappings, retries, deduplication, and audit;
   - private HTTP API consumed by the gateway.

2. **Fedify gateway**
   - public ActivityPub and WebFinger routes;
   - actor and object serving;
   - HTTP-signature handling;
   - inbound ActivityPub normalization;
   - signed outbound ActivityPub delivery.

Architecture references:

- [docs/architecture/overview.md](docs/architecture/overview.md)
- [docs/architecture/bridge-modes.md](docs/architecture/bridge-modes.md)
- [docs/architecture/event-flows.md](docs/architecture/event-flows.md)
- [docs/architecture/http-routes.md](docs/architecture/http-routes.md)
- [docs/architecture/database-map.md](docs/architecture/database-map.md)
- [docs/architecture/gateway-python-contract.md](docs/architecture/gateway-python-contract.md)
- [docs/development/navigation.md](docs/development/navigation.md)

## Tests

Python test suite:

```bash
source .venv/bin/activate
.venv/bin/pytest -q
```

Gateway checks:

```bash
cd fedify-gateway
npm run check
npm test
```

Test mechanisms and focused commands are documented in [docs/development/test-assurance.md](docs/development/test-assurance.md).

Deployment instructions are in [docs/DEPLOY.md](docs/DEPLOY.md).