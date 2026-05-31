# Future tasks journal

This journal collects deferred work discovered while planning or implementing bridge features. It is intentionally broader than a single implementation plan and should be updated when new future work appears.

## Source: local community user ban v1

## 1. Unban command

Status: implemented by plan 74. The command deactivates active rows, keeps responses ephemeral, uses owner/super-admin runtime checks, and does not expose inactive historical rows through no-active-ban errors.

Remaining future work belongs under the audit/history and pagination items below rather than this completed command stub.

## 2. List banned users command

Status: implemented by plan 74 for active bans. The command is public to invoke within a guild, returns ephemeral output, shows handles and reasons, and limits visible rows to the first 20 active bans.

Remaining future work: real pagination, inactive history listing, dashboard exposure decisions, and audit-log support.

## 3. Creator ownership persistence and enforcement

Status: planned separately in `plans/73_local_community_creator_ownership.md`. Keep this item here only as historical context until that plan is implemented and verified.

V1 cannot enforce “only the community creator can ban” because local communities do not yet persist the creator Discord user id.

Future work should add something like:

```text
local_communities.created_by_discord_user_id
```

Then update `/create_community` to populate it and update moderation commands so only the creator/owner of that community can use them.

This needs a migration/backfill decision for existing communities:

- require an operator to claim existing communities;
- backfill from historical command metadata if available;
- temporarily leave old rows ownerless and restrict owner-only commands until claimed.

## 4. Community autocomplete for moderation commands

Status: implemented for `/ban-user`, `/unban-user`, and `/list-banned-users` through plans 74 and 75.

The moderation commands now submit stable community slugs, use autocomplete as UX only, and keep runtime preconditions as the security boundary. Remaining autocomplete work belongs to future management commands such as `/edit-community`, disable/archive, and subscription approvals.

## 5. Discord-originated moderation

V1 only blocks inbound ActivityPub activities from remote actors. It does not ban Discord users and does not block Discord-originated publish/edit/delete actions.

A later plan should define whether a remote actor ban should have any effect on:

- Discord users posting into a bridge-hosted community;
- Discord users editing/deleting their own local-community surfaces;
- Discord users mapped to Fediverse identities later;
- local subscriber channels sending content into the community.

This should remain separate because Discord identity and Fediverse actor identity are different domains.

## 6. Existing subscriber cleanup

V1 does not remove or deactivate existing `RemoteSubscriber` rows when an actor is banned.

A later plan should decide what should happen when a banned actor already follows a community:

- leave the subscriber row as-is but ignore future activities;
- mark the subscriber inactive locally;
- send any federated Undo/Reject/Block-style activity if protocol research says that is correct;
- expose the existing subscription state to operators.

This also affects future Follow/Undo(Follow) semantics and must be tested separately.

## 7. Federated moderation activities

V1 does not send any outbound ActivityPub moderation object.

Out of scope for v1:

```text
Block
Reject
Undo
Delete
federated ban announcement
```

A later plan should research the correct ActivityPub/Fediverse behavior before implementing outbound federation for moderation actions. The project should avoid inventing protocol behavior that may conflict with Lemmy, Mastodon, or other Fediverse software expectations.

## 8. WebFinger and remote identity resolution

V1 does not use WebFinger or remote actor fetches. It accepts the same `user@example.com` handle shape that the bridge displays in Discord and does best-effort local extraction from incoming actor URLs.

Future work may add network resolution:

```text
user@example.com -> actor URL
```

Open decisions:

- whether ban command should fail when resolution fails;
- whether unresolved bans should remain pending;
- whether resolution should retry in background;
- timeout and rate-limit behavior;
- whether resolution is safe to use in operator commands but never in inbound hot paths.

## 9. Remote actor identity mapping table

V1 stores optional `actor_url` directly on the ban row as a small cache. It does not add a shared identity table.

A later plan may add a table such as:

```text
remote_actor_identities
- actor_handle
- actor_url
- webfinger_subject
- resolved_at
- last_seen_at
- resolution_status
```

This is useful if multiple features need reliable handle-to-actor mapping, for example:

- bans;
- dashboards;
- moderation audit logs;
- subscription approval flows;
- future actor profile displays.

## 10. Ban list in dashboard or public UI

V1 does not expose ban data in the public dashboard.

A later plan should decide whether ban data belongs in any UI at all. If it does, it should define:

- public vs operator-only visibility;
- whether reasons are sensitive;
- whether actor URLs should be shown;
- whether inactive historical bans are visible;
- how this interacts with dashboard redaction rules.

## 11. Inbound activity outcome tracking

V1 does not add reason-specific receipt statuses such as `ignored_by_ban`.

Future work should design explicit inbound activity outcome tracking if the project needs better observability.

Candidate statuses:

```text
processed
duplicate
ignored_by_ban
ignored_unknown_subscription
ignored_unmapped_context
failed
```

This should be a separate plan because it changes event observability, receipt semantics, tests, and possibly dashboard/debug tooling.

## 12. Ban audit/history model

V1 stores the active ban row and optional reason, but it does not define a full audit log.

Future work could add audit history for:

- ban created;
- duplicate ban attempted;
- unban applied;
- ban reason changed;
- actor URL cache filled;
- ownership/permission changes.

This should be separate from v1 unless there is a concrete operator requirement.

## 13. Ban reason editing

V1 duplicate ban attempts are rejected and do not update the reason.

Future work could add an explicit reason-editing command or allow `/ban-user` to update an existing reason under a flag. This should not be implicit in v1 because duplicate behavior is intentionally simple and explicit.

## 14. Better actor handle parsing

V1 uses the same handle format displayed in Discord and best-effort extraction from common actor URL shapes.

Future work should revisit this if the bridge needs broader Fediverse compatibility. Cases to evaluate:

- actor URLs with non-standard paths;
- usernames whose display casing differs from canonical actor id;
- instances where `preferredUsername` is not the final URL segment;
- actors whose canonical WebFinger handle differs from best-effort URL extraction.

## 15. Owner-only moderation command suite

User ban is only one moderation action. Once community ownership exists, a broader owner-only command suite should be planned coherently:

- `/ban-user`;
- `/unban-user`;
- `/list-banned-users`;
- community visibility changes;
- community disable/delete;
- subscription approval settings;
- manual subscriber approval/rejection.

## Source: local community creator ownership

## 16. Role system

A future plan may replace the current overloaded configured list with a real role model.

The role model should separate at least these concerns:

- who can create communities;
- who is a bridge-level super-admin;
- who can moderate a community;
- who can edit community settings.

## 17. Dedicated super-admin configuration name

The current config key remains `local_community_operator_allowlist`, but plan 73 treats it as the super-admin list for management commands.

A future migration may rename or split config keys once role policy is clearer.

Open details:

- whether to preserve backward compatibility with the old key;
- whether to support separate creator and super-admin allowlists;
- how to document the transition for existing deployments.

## 18. Legacy ownership claim/backfill

Plan 73 does not add `/claim-community` or automatic backfill.

Existing NULL-owned communities remain super-admin-managed until a separate plan defines claim, transfer, audit, and conflict behavior.

Open details:

- who is allowed to claim a legacy community;
- whether claim should require super-admin approval;
- whether ownership can be inferred from historical command metadata;
- how to prevent two operators from claiming the same legacy community incorrectly.

## 19. Ownership transfer and multiple moderators

Plan 73 stores exactly one creator owner. It does not support transfer, co-owners, moderators, or per-community ACLs.

A later plan should define whether the project needs:

- owner transfer;
- additional community moderators;
- temporary moderators;
- per-command permissions;
- owner removal or recovery when the owner leaves the Discord server.

## 20. Edit community command

Status: local metadata editing is planned by plan 76.

`/edit-community` v1 edits only display name and summary through a Discord modal. It uses owner-or-super-admin runtime preconditions, keeps edits local-only, and allows clearing summary to NULL. Broader community settings remain future work.

## 21. Unban and ban-list commands after ownership

Status: implemented by plan 74.

`/unban-user` deactivates active rows without deleting moderation history. `/list-banned-users` shows active bans with reasons in an ephemeral response. Both commands use guild-aware runtime preconditions and autocomplete helpers.

## 22. Community autocomplete for future management commands

Status: moderation command autocomplete is implemented by plans 74 and 75.

Future management commands should follow the same pattern: autocomplete lists context-appropriate communities, submitted values remain stable community slugs, and runtime preconditions remain the security boundary. This still applies to future commands such as `/edit-community`, disable/archive, subscription approvals, and any broader role-system command surface.

## 23. Dynamic Discord command visibility

Plan 73 does not dynamically hide `/ban-user`.

A future UX plan may expose management commands only to super-admins and users who own at least one local community.

This must remain best-effort only because:

- command visibility is not the security boundary;
- ownership checks depend on command input;
- stale Discord command permission state must not grant access.

## 24. Dashboard ownership display

Plan 73 does not expose owner ids in the dashboard.

A later UI plan should decide whether owner information belongs there.

Open details:

- whether raw Discord user ids should ever be displayed;
- whether owner display belongs only in operator-only views;
- whether owner ids should be redacted in public dashboard output.

## 25. Audit log for management actions

Ban rows already store who created a ban, but plan 73 does not add a broader audit model.

A future audit model could cover:

- owner changes;
- failed authorization attempts;
- unban actions;
- edit-community actions;
- role and super-admin changes;
- legacy claim/backfill actions.

## 26. Federated community metadata updates

`/edit-community` v1 is local-only. A later plan should research and implement outbound ActivityPub/Lemmy-compatible metadata updates when a local community display name or summary changes.

Open details:

- which ActivityPub object/activity shape compatible servers expect;
- whether updates should be sent to all remote followers or only selected inboxes;
- how to sign and deduplicate metadata update deliveries;
- how failures should be retried or surfaced to operators;
- whether local actor routes need explicit cache-control changes.

## 27. Richer community settings editing

After `/edit-community` v1, a later plan should decide how to edit settings beyond display metadata.

Candidate fields:

- visibility or subscription policy;
- disabled/enabled/archive state;
- Discord forum binding changes;
- ownership transfer or moderator assignment.

These should not be mixed into the local metadata edit because they affect lifecycle, authorization, routing, and possibly federation behavior.
