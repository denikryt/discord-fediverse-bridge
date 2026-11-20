# Fluxer-only #12 — follow-up to ED01–ED03 review

The review of `7ae61f9` confirmed the prior corrections but identified
forum Create's unverified outbound guild hint and noncanonical numeric IPv4
origins. These corrections preserve the exclusive Fluxer runtime, current
CreateAttempt/SQLite ownership and private Node SDK boundary. They do not
activate canonical Fluxer federation or complete the S19 live acceptance.

## ED04 — verified physical channel on Create and send

- `_preflight()` returns the actual `ChannelRef` and current applied tag IDs.
  `create_forum_thread()` uses this scope for `guild_id`, exact receipt
  comparison and the returned `ThreadRef`; no second channel read is added.
- `send_message(ChannelRef)` uses the same live guild instead of optional
  caller metadata. Thread sends continue using their independent verified
  thread scope, including the immediate reply parent.
- A cross-process Python adapter → authenticated production Node dispatcher →
  SDK-boundary fake verifies forum Create with missing, correct and foreign
  guild hints, one successful SDK write, exact starter and applied tags.
  A separate scoped RPC fixture verifies regular channel send and mismatch.

## ED05 — canonical IPv4 origins

- Python `canonical_origin()` rejects any would-be IPv4 authority with a
  numeric/hex terminal label unless `ipaddress.IPv4Address` accepted the
  original canonical spelling. Node `validateOrigin()` refuses a raw unbracketed
  host when WHATWG URL would change its spelling during canonicalization.
- Negative tests cover `127.1`, `0x7f000001`, `0177.0.0.1`, `2130706433`
  and `127.0.0.01`. Canonical `127.0.0.1`, ordinary DNS, ASCII A-labels,
  standard ports and explicit loopback development remain valid.
- These guards run at configuration/discovery boundaries before persisted
  SQLite binding and SDK admission; their responses never contain secrets.

## Verification

- TDD negative cases reproduced both defects before changes.
- Targeted Python forum/Create/config/SQLite/RPC suite: **85 PASS**.
- Targeted Node discovery/dispatch/send/forum/permissions: **38 PASS**.
- Fluxer Node full executable sweep: **125 PASS**, two test files failed to
  load because `@fluxerjs/core` is absent, not due to assertion failures.
- SDK lock check: PASS. Full `pytest` blocked by missing `hypothesis`; Node
  TypeScript check blocked by missing `@types/node`.
- Real Fluxer REST/Gateway and Docker Compose **NOT RUN**; issue #12 S19 is
  still pending operator-provided server, bot credentials and Docker engine.
