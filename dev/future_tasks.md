# Future tasks journal

This journal collects deferred work discovered while planning or implementing bridge features.

## 1. Role system

Replace the current overloaded configured user list with a real role model.

The role model should separate at least these concerns:

- who can create communities;
- who is a bridge-level super-admin;
- who can moderate a community;
- who can edit community settings;
- whether roles are global, guild-scoped, or community-scoped.

## 2. Dedicated super-admin configuration

The current configuration uses an existing operator allowlist as the super-admin list for management commands.

A future migration may rename or split configuration keys once role policy is clearer.

Open details:

- whether to preserve backward compatibility with the old key;
- whether to support separate creator and super-admin allowlists;
- how to document the transition for existing deployments.

## 3. Legacy ownership claim/backfill

Existing communities without a stored owner still need an explicit claim or backfill workflow if they should become owner-managed instead of only super-admin-managed.

Open details:

- who is allowed to claim a legacy community;
- whether claim should require super-admin approval;
- whether ownership can be inferred from historical command metadata;
- how to prevent two operators from claiming the same legacy community incorrectly.

## 4. Ownership transfer and multiple moderators

The project stores one creator owner per local community. It does not yet support transfer, co-owners, moderators, or per-community ACLs.

A later design should define whether the project needs:

- owner transfer;
- additional community moderators;
- temporary moderators;
- per-command permissions;
- owner removal or recovery when the owner leaves the Discord server.

## 5. Dynamic Discord command visibility

Management command visibility could be improved so commands are shown only to super-admins and users who own at least one local community.

This must remain best-effort only because:

- command visibility is not the security boundary;
- ownership checks depend on command input;
- stale Discord command permission state must not grant access.

## 6. Management autocomplete consistency

Future management commands should follow the established autocomplete pattern:

- submitted values remain stable community slugs;
- autocomplete lists context-appropriate communities;
- runtime preconditions remain the security boundary;
- super-admin cross-guild behavior is explicit per command.

This applies to future command surfaces such as subscription approvals, ownership transfer, and role-management commands.

## 7. Discord/Fediverse identity moderation model

Remote actor bans currently operate on Fediverse actor identity. The project still needs a separate design for Discord-originated moderation and future identity mapping.

A later design should define whether a remote actor ban should have any effect on:

- Discord users posting into a bridge-hosted community;
- Discord users editing or deleting their own local-community surfaces;
- Discord users mapped to Fediverse identities later;
- local subscriber channels sending content into the community.

This should remain separate because Discord identity and Fediverse actor identity are different domains.

## 8. Existing subscriber cleanup after actor bans

Banning a remote actor does not remove or deactivate existing remote subscriber rows.

A later design should decide what should happen when a banned actor already follows a community:

- leave the subscriber row as-is but ignore future activities;
- mark the subscriber inactive locally;
- send any federated Undo, Reject, Block, or compatible activity if protocol research says that is correct;
- expose the existing subscription state to operators.

This also affects future Follow and Undo(Follow) semantics.

## 9. Federated moderation activities

The bridge does not yet send outbound ActivityPub moderation objects for local moderation actions.

Future research should define compatible behavior for actions such as:

```text
Block
Reject
Undo
Delete
federated ban announcement
```

The project should avoid inventing protocol behavior that may conflict with Lemmy, Mastodon, or other Fediverse software expectations.

## 10. WebFinger and remote identity resolution

Remote actor moderation currently accepts the same `user@example.com` handle shape shown in Discord and uses local best-effort extraction from actor URLs.

Future work may add network resolution:

```text
user@example.com -> actor URL
```

Open decisions:

- whether a ban command should fail when resolution fails;
- whether unresolved bans should remain pending;
- whether resolution should retry in background;
- timeout and rate-limit behavior;
- whether resolution is safe to use in operator commands but never in inbound hot paths.

## 11. Remote actor identity mapping table

Actor URL data is cached directly on ban rows when available. The project does not yet have a shared remote actor identity table.

A later design may add a table such as:

```text
remote_actor_identities
- actor_handle
- actor_url
- webfinger_subject
- resolved_at
- last_seen_at
- resolution_status
```

This would be useful if multiple features need reliable handle-to-actor mapping, for example:

- bans;
- dashboards;
- moderation audit logs;
- subscription approval flows;
- future actor profile displays.

## 12. Ban-list pagination and history views

The ban-list command shows active bans only and limits output to a small number of rows.

Future work should decide how to expose:

- pagination for large active ban lists;
- inactive historical bans;
- filtering by actor handle or reason;
- operator-only vs public visibility.

## 13. Ban list in dashboard or public UI

The public dashboard does not expose ban data.

A later UI design should decide whether ban data belongs in any dashboard view. If it does, it should define:

- public vs operator-only visibility;
- whether reasons are sensitive;
- whether actor URLs should be shown;
- whether inactive historical bans are visible;
- how this interacts with dashboard redaction rules.

## 14. Ban reason editing

Duplicate active ban attempts are rejected and do not update the reason.

Future work could add an explicit reason-editing command or allow a ban command to update an existing reason under a flag. This should be explicit rather than an accidental duplicate-ban side effect.

## 15. Better actor handle parsing

Current actor-handle parsing is intentionally best-effort. It works for common actor URL shapes but is not a full Fediverse identity resolver.

Cases to evaluate:

- actor URLs with non-standard paths;
- usernames whose display casing differs from canonical actor id;
- instances where `preferredUsername` is not the final URL segment;
- actors whose canonical WebFinger handle differs from best-effort URL extraction.

## 16. Federated community metadata updates

Community metadata edits are local-only.

A later design should research and implement outbound ActivityPub or Lemmy-compatible metadata updates when a local community display name, summary, or lifecycle-relevant metadata changes.

Open details:

- which ActivityPub object or activity shape compatible servers expect;
- whether updates should be sent to all remote followers or only selected inboxes;
- how to sign and deduplicate metadata update deliveries;
- how failures should be retried or surfaced to operators;
- whether local actor routes need explicit cache-control changes.

## 17. Richer community settings editing

Community editing currently covers metadata and lifecycle status fields only.

Future settings may include:

- visibility or subscription policy;
- Discord forum binding changes;
- ownership transfer or moderator assignment;
- per-community moderation defaults;
- subscription approval mode.

These should be designed separately because they affect lifecycle, authorization, routing, and possibly federation behavior.

## 18. Federated disabled-community behavior

Disabled local communities currently use local-only behavior.

A later design should research and implement compatible federation behavior.

Open details:

- whether remote Follow should receive Reject instead of local ACK-and-skip;
- whether disabling should emit an ActivityPub Update, Delete, Tombstone-like object, or no outbound activity;
- which inboxes should receive lifecycle updates;
- how failures should be retried or surfaced to operators;
- how this interacts with re-enable.

## 20. Subscriber cleanup on disabled communities

Disabling a community leaves existing local and remote subscriber rows untouched. Fanout is blocked while disabled and resumes after re-enable.

A later design should decide whether disable should optionally deactivate subscribers, notify them, or require resubscription after re-enable.

## 21. Disabled-community dashboard UI

No dedicated dashboard UI exists for disabled communities.

A later UI design should decide whether disabled communities are visible publicly, visible only to operators, hidden from normal lists, or shown with a lifecycle badge.

## 22. Extended management audit scope

The backend now records v1 audit rows for community creation, metadata/status edits, ban/unban mutations, and selected authorization denials.

Later audit extensions may cover:

- ownership transfer or moderator assignment;
- ban reason edits without status changes;
- role and super-admin configuration changes;
- legacy claim/backfill actions;
- operator-facing audit search, retention, export, or dashboard UI.

## 24. Remote community history backfill and content archive

The bridge stores identifier mappings but does not persist enough post and comment content to reconstruct remote community history.

Open details:

- whether to use ActivityPub outboxes and replies, Lemmy APIs, or a dedicated archive endpoint;
- which post and comment fields must be stored locally;
- how backfilled content is deduplicated against live federation;
- how edits, deletes, pagination, and partial retries are handled;
- whether restored history should be published to Discord.

## 25. Production-ready Docker deployment setup

The project does not yet have a supported production Docker deployment.

Open details:

- production images for the Python bridge and Fedify gateway;
- Compose configuration, persistent volumes, secrets, and health checks;
- database migrations, reverse proxy, TLS, backups, upgrades, and rollback;
- one project version shared by both services;
- Git tags, immutable image tags, and explicit version selection during deployment.


## 27. Dashboard active-user statistics

The dashboard does not currently show activity metrics such as daily or monthly active users.

A later design should define what counts as an active user, which bridge or Discord/Fediverse events are authoritative, how metrics are aggregated over time, and whether statistics are shown per guild, per community, or for the whole instance.
