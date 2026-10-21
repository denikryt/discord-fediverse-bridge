# PR #20 follow-up verification

Date: 2026-10-20. Base: `05ef10427d91caf4c3dbd8e9ff621d7c169886dc`.
All changes and tests ran in detached worktree
`/tmp/discord-adapter-verification-20`. No push, merge, deployment, or
production Docker, network, volume, database, or credential operation ran.

## Result

The three additional Node failures introduced during the prior Fedify 2.2.19
review are resolved. The full Node file enumeration now has exactly the seven
pre-existing baseline failures and no new failing files. Release readiness is
still **not verified** because those seven failures remain, public-peer HTTPS
and live Discord integration were outside this isolated review, and the prior
full Python suite retained 24 baseline failures.

## Changes and findings

- The supplied HTTP 406 test patch was applied. With explicit
  `Accept: application/activity+json`, old `/actors/alice` and `/users/alice`
  URLs return 404 on Fedify 2.2.19; `verify-user-canonical-actor.ts` passes.
- Both Follow fixtures previously received 202 because Fedify queued the
  signed request. The worker later failed DNS validation for synthetic
  `remote.example` before its global `fetch` stub ran; `getActor()` returned
  `null` and no `local.follow_requested` event was emitted. Actor-specific
  inbox signature verification also uses an authenticated document loader.
  The two tests now supply isolated standard and authenticated document
  loaders. They run with signature verification enabled and pass, including
  unsigned rejection and actor-document mismatch cases. Production config
  supplies neither fixture loader.
- The supplied same-ID probe reproduced content substitution at the old
  helper boundary (`substitutionAccepted: true`, exit 1). The verifier now
  compares the entire nested payload with the Fedify-accepted typed Announce,
  and the listener uses the accepted delivery payload instead of a mutable
  cache row indexed by outer ID. Author-origin verification remains required.
  A new helper regression failed before the fix and passes afterward.
- A signed HTTP Announce was admitted through the composed Hono/Fedify handler
  with signature verification active and held in its real queue. A later
  invalid-signature request reused both IDs, passed Hono's header-presence
  prefilter, and replaced the raw cache row before Fedify rejected it with 401.
  Starting the worker delivered the
  original signed content to the Python HTTP boundary. Temporarily restoring
  the old cache read made this test fail; the safe read was restored.

## Test evidence

| Check | Result |
| --- | --- |
| `npm run check` | Passed |
| `npm run test:security-boundaries` | 20 passed, 0 failed |
| `npm test` | Stops at the baseline `verify-follow-activity-id.ts` failure after checks/security pass |
| All `tests/verify-*.ts` independently | 50 files, 43 passed, 7 baseline failures |
| Canonical actor, both Follow workflows, signed Announce ingress | Passed |
| `git diff --check` | Passed |

The seven remaining failing files are `verify-follow-activity-id`,
`verify-local-community-canonical-key`, `verify-local-community-publish`,
`verify-local-community-relay`, `verify-local-community-update-delete`,
`verify-signed-json-delivery`, and
`verify-user-actor-profile-update-delivery`. Their failure types match the
baseline recorded in `verification_worklog.md`: one missing fixture method,
one 406/404 assertion, four transport/DNS or delivery assertions, and one
profile delivery-state assertion. Per-file logs are in
`/tmp/discord-node-suite-20/`.

## Limits and compatibility

The signed ingress test uses synthetic documents and a fake Python HTTP
boundary; it does not prove behavior against a public relay. Fedify 2.2.19
keeps the original accepted payload on its inbox context; the listener uses
that runtime field and falls back to typed Announce serialization if the field
is unavailable. This dependency should be checked when Fedify is upgraded.
The previous review's Python test and audit findings were not rerun in this
follow-up; see the worklog for their original evidence.
