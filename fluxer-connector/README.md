# Fluxer Node connector (under implementation)

This Node package provides the authenticated, loopback-only Python ↔ Node RPC
boundary and the tokenless, SSRF-checked Fluxer discovery preflight. The SDK
session owner in `src/sdk_session.ts` is one self-contained lifecycle component:
it observes Gateway READY, checks the exact discovered `api_public` and actual
REST base URL when exposed, verifies that the authenticated principal is a bot,
and drains/disposes its one client on failure or stop. Errors returned at this
boundary do not contain provider exception text or credentials.

The SDK session intentionally accepts a **trusted transport factory**. Preflight
of `/.well-known/fluxer` alone does not secure subsequent SDK-created HTTP or
WebSocket sockets against DNS rebinding, redirects or unapproved egress. The
production factory and network policy must be completed and verified before
this lifecycle owner is connected to a production bot. This component does not
claim to support forum REST writes, Gateway normalization or real-instance
acceptance; those remain separate implementation steps. Never enable a bot
login by passing an unrestricted `Client.fromDiscovery()` factory without the
required SDK egress protections.

Run `npm test` with Node 22.13 or newer for the deterministic fake-provider,
HTTP and lifecycle behavior suites. The pinned `@fluxerjs/core` package and
`@types/node` must be installed before a normal `npm run check` or real SDK
integration test can be considered verified. The current offline development
environment can type-check the TypeScript source with an externally installed
Node type declaration directory, but that is **not** verification of the SDK
package's types or wire behavior.
