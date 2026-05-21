# Known Issues

## Open

1. **Unfollow does not stop remote events**
   Remote actors may continue sending activities after the bridge sends an unfollow/undo-follow request. Need to inspect the exact outgoing `Undo(Follow)`, target inbox, signer, and local subscription state.

2. **Mastodon direct replies to remote Lemmy parents do not resolve community**
   Direct `Create(Note)` replies from Mastodon to local bridge parents now work via parent mapping. Replies where `inReplyTo` points to a remote Lemmy object, for example `https://lemmy.nu31.space/comment/223`, still fail community resolution.

3. **Relay/fanout of Mastodon-origin replies to Lemmy fails**
   Mastodon-origin replies currently relay as Mastodon-shaped embedded activities and Lemmy returns `400 Bad Request`. This is separate from Discord mirroring.

4. **Outer Announce activities are not fetchable**
   `Create.id` is now fetchable and Mastodon import works, but outer `Announce.id` is still not served publicly.

## Known behavior

1. **Mastodon shows Lemmy-style Page posts as title + original link**
   This matches observed Lemmy/Mastodon behavior. Comments are `Note` objects and display full body text.
