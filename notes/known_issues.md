# Known issues and verified behavior

- Valid inbound activities from communities with no accepted bridge subscription are ACKed at the ActivityPub layer and skipped locally unless they relate to already mapped bridge context.
- Local community Undo(Follow) removes the follower row and future local-community fanout uses current accepted followers as the delivery source of truth.
