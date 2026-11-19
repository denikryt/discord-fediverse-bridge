# Federation

The bridge connects Discord forum channels and user-owned feeds to selected
ActivityPub communities and Person actors. Its federation is intended for Lemmy and
other threadiverse software, NodeBB categories, and selected Mastodon-style
replies. It is not a general-purpose ActivityPub server.

## Protocols

- [ActivityPub](https://www.w3.org/TR/activitypub/) server-to-server delivery
  and [ActivityStreams 2.0](https://www.w3.org/TR/activitystreams-core/).
- [WebFinger](https://www.rfc-editor.org/rfc/rfc7033) for local actor discovery.
- HTTP signatures for authenticated inbox delivery. The gateway uses Fedify
  for signature verification and signing.
- Group federation conventions used by the threadiverse; the bridge does not
  claim complete [FEP-1b12](https://codeberg.org/fediverse/fep/src/branch/main/fep/1b12/fep-1b12.md) support.

## Actors and discovery

| Actor | Type | Canonical URL | Use |
| --- | --- | --- | --- |
| Bridge | `Service` | `/actors/services/{identifier}` | Follows remote communities on behalf of mapped Discord forums. |
| Registered user | `Person` | `/actors/users/{username}` | Authors Discord-originated posts/replies and follows remote Person actors. |
| Local community | `Group` | `/actors/communities/{slug}` | Represents a Discord-backed community and distributes its activity. |

Actors advertise their inbox, outbox, followers, shared inbox, and public keys.
The shared inbox is `/inbox`; actor inboxes are under each actor URL. WebFinger
resolves `acct:username@host` and `acct:!community@host`. A plain community
handle also resolves when no local user owns that name. The bridge-specific
`/.well-known/discord-fediverse-bridge/communities` endpoint lists public local
communities for discovery.

User outboxes expose supported stored activities. User and community followers
collections expose accepted followers. Service followers and outbox, and the
community outbox, are empty collections.

## Supported activities

Support is limited to mapped communities, registered users, and applicable
federation and community policies; it is not generic inbox processing.

| Activity | Incoming | Outgoing |
| --- | --- | --- |
| `Follow`, `Undo(Follow)` | Follow or unfollow a local community or user. | Bridge Service follows remote communities; individual registered Person actors independently follow/unfollow remote Person actors. |
| `Accept(Follow)`, `Reject(Follow)` | Reconcile signed responses to a current user-owned remote Person Follow; community follows use their existing handler. | Accept a Follow of a local community or user. |
| `Create(Page\|Article\|Note)` | Supported community posts and replies, public root Notes from accepted remote Person follows, and replies to retained Person posts. | User-authored `Page`/`Note` content and Person `Note` replies to retained conversations. |
| `Update`, `Delete` | Update or delete supported mapped content from its authorised owner. | Propagate supported content edits and deletions; user profile changes send `Update(Person)`. |
| `Announce` | Community distribution of supported `Create`, `Update`, or `Delete`; NodeBB also uses `Announce` of a `Note`/`Article` or its URL. | A local Group distributes supported creates, updates, and deletes to followers. |

Discord users must register before their posts or replies are federated. A
remote community Follow belongs to the bridge Service; a remote Person Follow
belongs to the subscribing registered Person actor. Content belongs to its
registered Person author. For locally hosted communities, a remote bridge's
accepted Service may authorise its own registered Person actors; content
updates and deletes still require the original owner.

For already mapped remote Community content, an authenticated Group-distributed
`Announce(Update/Delete)` may update **only that Group's Discord-facing view**.
It does not acquire the original Person's authorship or delete the original
Person Note. A first-Create provenance snapshot remains immutable; the current
Group view and terminal Group tombstone are stored on the canonical mapping.
A processed inbound mutation receipt confirms that SQLite committed a new
revision, **not** that every Discord destination is up to date. Each target
retains an applied revision and retry state. New bot-created Community
roots/comments carry exact identity markers for proof-based reconciliation
of lost Discord POST acknowledgements; an unproven legacy `unknown` is never
blindly re-POSTed.

## Interoperability

- **Lemmy and threadiverse:** remote communities are `Group` actors; posts
  typically use `Page` and comments use `Note`. Community fanout uses
  `Announce`.
- **NodeBB:** categories are discovered through the categories API and
  verified as ActivityPub `Group` actors before following. Topics and replies
  use `Note`; the bridge uses `inReplyTo` and verified Group mappings to
  associate replies with topics. Outgoing NodeBB posts and replies include
  their conversation `context`.
- **Mastodon / remote Person:** registered users have public profiles and can
  publish Personal Feed Notes. Each local Person can Follow a remote Person
  and route newly received public Notes into one or more Discord text channels.
  The received remote Note's root is shown with the original handle/avatar via
  a bot-owned Incoming webhook without duplicating the author in the root
  body. The complete canonical source URL remains the root's last line; bot
  comments use a fixed identity marker and native Replies.
  Signed replies from registered Discord users use the same canonical Person
  identity, stable `inReplyTo`, and original explicit direct-recipient actor IDs
  in the ActivityStreams audience. This is not full Mastodon compatibility.

A remote NodeBB Follow needs an explicit signed `Accept`; HTTP success for a
Follow does not mean it was accepted. A locally queued publication likewise
only confirms local admission. Remote HTTP success does not guarantee that a
peer displayed the content: during live testing NodeBB returned success for a
post it rejected under its minimum-length rule.

## Delivery and limitations

The bridge stores mappings and delivery state to avoid loops and duplicate
sends. A delivery whose network outcome is unknown is not blindly retried;
remote delivery is not exactly once. Follow recovery requires confirmation of
the remote state before any local subscription state is changed.

Remote Person feeds do not crawl timelines or retrieve missing earlier
comments, and do not forward non-public or followers-only content to Discord.
Person Follow/Undo, delivery and cancellation have separate durable transport
states: `uncertain` means the HTTP outcome cannot be proved and is not blindly
resent. A Discord webhook root's original webhook identity is retained for
subsequent edits/deletes; removing that webhook prevents those operations.

The bridge does not implement ActivityPub client-to-server, NodeInfo, generic
boost handling, voting, likes, polls, reactions, or historical Discord channel
backfill. Discord attachment federation is not a complete media pipeline.
Delivery and deletion on independent remote servers cannot be guaranteed.

For setup and operation, see [deployment](DEPLOY.md). For routing and
registration boundaries, see the [README](../README.md).
