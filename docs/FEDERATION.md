# Federation

The bridge connects Discord forum channels and personal feeds to selected
ActivityPub communities and actors. Its federation is intended for Lemmy and
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
| Registered user | `Person` | `/actors/users/{username}` | Authors Discord-originated publications and personal-feed content. |
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
| `Follow`, `Undo(Follow)` | Follow or unfollow a local community or user. | Bridge Service follows or unfollows a remote community. |
| `Accept(Follow)` | Accept a pending remote community Follow. | Accept a Follow of a local community or user. |
| `Create(Page\|Article\|Note)` | Supported community posts and replies, and personal-feed replies. | User-authored posts and replies: `Page` for threadiverse posts, `Note` for replies and NodeBB topics. |
| `Update`, `Delete` | Update or delete supported mapped content from its authorised owner. | Propagate supported content edits and deletions; user profile changes send `Update(Person)`. |
| `Announce` | Community distribution of supported `Create`, `Update`, or `Delete`; NodeBB also uses `Announce` of a `Note`/`Article` or its URL. | A local Group distributes supported creates, updates, and deletes to followers. |

Discord users must register before their posts or replies are federated. A
remote community Follow belongs to the bridge Service; content belongs to its
registered Person author. For locally hosted communities, a remote bridge's
accepted Service may authorise its own registered Person actors; content
updates and deletes still require the original owner.

## Interoperability

- **Lemmy and threadiverse:** remote communities are `Group` actors; posts
  typically use `Page` and comments use `Note`. Community fanout uses
  `Announce`.
- **NodeBB:** categories are discovered through the categories API and
  verified as ActivityPub `Group` actors before following. Topics and replies
  use `Note`; the bridge uses `inReplyTo` and verified Group mappings to
  associate replies with topics. Outgoing NodeBB posts and replies include
  their conversation `context`.
- **Mastodon:** registered user profiles and selected `Note` replies to local
  communities are supported. This is not full Mastodon compatibility.

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

The bridge does not implement ActivityPub client-to-server, NodeInfo, generic
boost handling, voting, likes, polls, reactions, or historical Discord channel
backfill. Discord attachment federation is not a complete media pipeline.
Delivery and deletion on independent remote servers cannot be guaranteed.

For setup and operation, see [deployment](DEPLOY.md). For routing and
registration boundaries, see the [README](../README.md).
