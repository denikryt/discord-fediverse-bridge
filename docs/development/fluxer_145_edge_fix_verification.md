# Fluxer-only #12 — edge re-review corrections (ED01–ED03)

The additional review of `e363af8` identified three local adapter/configuration
inconsistencies. These checkpoints change only the physical transport and its
startup contracts; they do **not** enable Fluxer ActivityPub community publication
or claim real-server acceptance.

## Corrections

- **ED01 / thread write guild.** The adapter reads and validates the immediate
  thread/parent exactly once during write preflight and passes the confirmed
  `guild_id` in the outbound RPC. Missing caller guild metadata is allowed;
  contradictory metadata fails before an SDK write. The immediate reply parent
  is still fetched and checked.
- **ED02 / origin.** Python config and Node discovery accept ASCII A-label
  hostnames and reject raw Unicode hostnames. This deliberately avoids the
  Python IDNA-2003 / WHATWG URL disagreement for `faß.test`; different ASCII
  hostname identities are still distinct.
- **ED02 / private bearer.** The operator bearer must be at least eight
  visible ASCII bytes (0x21–0x7e), with no spaces or control characters.
  The validation applies at both settings/Node startup and direct Python/Node
  RPC ownership boundaries, before HTTP headers or physical writes.

## Test boundaries

The cross-process Python-to-authenticated-Node SDK-boundary harness verifies
missing, matching and contradictory guild metadata, nested replies, canonical
A-label READY attestation and negative cross-origin readiness. Additional
configuration and RPC tests exercise 7/8-byte bearer limits, Unicode, controls,
spaces, and direct-constructor bypasses. No live provider SDK/REST/Gateway
instance or real Compose deployment has been used for these corrections;
**S19 remains pending** even when local suites pass.

## Locally observed checks

- `unittest discover` across ten Fluxer transport/config/SQLite/Gateway modules:
  **108 PASS**; forum SDK-boundary cross-process fixture: **18 PASS**.
- Fluxer Node complete executable test sweep: **124 PASS**, **2 test files did not load**
  (`message_rest_contracts.test.mjs`, `sdk_write_contracts.test.mjs`, missing
  `@fluxerjs/core`). These are environment blockers, not passing tests.
- Fluxer SDK lock verification: **PASS**.
- Complete Python `pytest`: **BLOCKED** by missing `hypothesis` during `conftest`.
- Fluxer `tsc --noEmit`: **BLOCKED** by missing `@types/node` in this environment.
- Changes are not verified against a real Fluxer REST/Gateway server or Docker
  Compose. S19 still requires those tests with installed lockfile dependencies.
