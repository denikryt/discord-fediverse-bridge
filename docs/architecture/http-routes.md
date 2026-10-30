# HTTP routes

This document explains HTTP route ownership between the Python bridge and the Fedify gateway. It owns route/process/publicness information, especially for public-host routing decisions, and does not describe full runtime behavior.

| Path | Method | Owner process | Public or internal | Purpose | Primary source file | Public-host nginx owner |
| --- | --- | --- | --- | --- | --- | --- |
| `/` | GET | Python FastAPI | public | Public bridge dashboard HTML | `src/http_api.py` | Python |
| `/healthz` | GET | Python FastAPI | health | Python health when directly exposed | `src/http_api.py` | Gateway by default |
| `/register` | GET | Python FastAPI | public | Registration page | `src/http_api.py` | Python |
| `/auth/discord/start` | GET | Python FastAPI | public | Start Discord OAuth | `src/http_api.py` | Python |
| `/auth/discord/callback` | GET | Python FastAPI | public | OAuth callback | `src/http_api.py` | Python |
| `/register/complete` | POST | Python FastAPI | public | Complete registration | `src/http_api.py` | Python |
| `/register/success` | GET | Python FastAPI | public | Registration success page | `src/http_api.py` | Python |
| `/dashboard` | GET | Python FastAPI | public | Legacy redirect to root dashboard | `src/http_api.py` | Python |
| `/dashboard/static/dashboard.css` | GET | Python FastAPI | public | Dashboard stylesheet | `src/http_api.py` | Python |
| `/dashboard/static/dashboard.js` | GET | Python FastAPI | public | Dashboard browser logic | `src/http_api.py` | Python |
| `/dashboard/data` | GET | Python FastAPI | public | Public bridge dashboard JSON | `src/http_api.py` | Python |
| `/internal/activitypub/events` | POST | Python FastAPI | private | Gateway-to-Python AP event intake | `src/http_api.py` | Not publicly routed |
| `/internal/fedify/*` | GET/POST | Python FastAPI | private | Authenticated Gateway read models and signing keys | `src/internal_fedify_api.py` | Not publicly routed |
| `/internal/fluxer/events` | POST | Python FastAPI, opt-in only | bearer authenticated on existing listener | Connector-matched normalized Fluxer Gateway DTO; handler gates AP publication until #13 | `src/internal_fluxer_api.py`, `src/fluxer_ingress.py` | Not publicly routed; network isolation not guaranteed when bridge binds `0.0.0.0` |
| `/healthz` | GET | Fedify gateway | public | Gateway health | `fedify-gateway/src/routes/health.ts` | Gateway |
| `/.well-known/webfinger` | GET | Fedify gateway | public | Actor discovery | `fedify-gateway/src/routes/public-discovery.ts` | Gateway |
| `/.well-known/discord-fediverse-bridge/communities` | GET | Fedify gateway | public | Bridge-owned public local-community discovery | `fedify-gateway/src/routes/public-discovery.ts` | Gateway |
| `/inbox` | POST | Fedify gateway | public AP | Inbound ActivityPub with pre-Fedify request logging | `fedify-gateway/src/federation.ts`, `fedify-gateway/src/routes/inbox-logging.ts` | Gateway |
| `/actors/services/:identifier` | GET | Fedify gateway | public AP | Bridge service actor | `fedify-gateway/src/federation.ts`, `fedify-gateway/src/routes/public-activitypub.ts` | Gateway |
| `/actors/services/:identifier/inbox` | POST | Fedify gateway | public AP | Service actor-specific inbox | `fedify-gateway/src/federation.ts` | Gateway |
| `/actors/services/:identifier/outbox` | GET | Fedify gateway | public AP | Service actor outbox | `fedify-gateway/src/routes/public-activitypub.ts` | Gateway |
| `/actors/services/:identifier/followers` | GET | Fedify gateway | public AP | Service actor followers collection | `fedify-gateway/src/routes/public-activitypub.ts` | Gateway |
| `/actors/users/:username` | GET | Fedify gateway | public AP | Local user actor | `fedify-gateway/src/federation.ts`, `fedify-gateway/src/routes/public-activitypub.ts` | Gateway |
| `/actors/users/:username/inbox` | POST | Fedify gateway | public AP | User actor-specific inbox | `fedify-gateway/src/federation.ts` | Gateway |
| `/actors/users/:username/outbox` | GET | Fedify gateway | public AP | User outbox | `fedify-gateway/src/routes/public-activitypub.ts` | Gateway |
| `/actors/users/:username/followers` | GET | Fedify gateway | public AP | User followers collection | `fedify-gateway/src/routes/public-activitypub.ts` | Gateway |
| `/actors/communities/:slug` | GET | Fedify gateway | public AP | Local community actor | `fedify-gateway/src/federation.ts`, `fedify-gateway/src/routes/public-activitypub.ts` | Gateway |
| `/actors/communities/:slug/inbox` | POST | Fedify gateway | public AP | Community actor-specific inbox | `fedify-gateway/src/federation.ts` | Gateway |
| `/actors/communities/:slug/outbox` | GET | Fedify gateway | public AP | Community outbox | `fedify-gateway/src/routes/public-activitypub.ts` | Gateway |
| `/actors/communities/:slug/followers` | GET | Fedify gateway | public AP | Community followers collection | `fedify-gateway/src/routes/public-activitypub.ts` | Gateway |
| `/follow-community` | POST | Fedify gateway | internal | Send Follow | `fedify-gateway/src/routes/internal-follow.ts` | Internal/gateway |
| `/unfollow-community` | POST | Fedify gateway | internal | Send Undo(Follow) | `fedify-gateway/src/routes/internal-follow.ts` | Internal/gateway |
| `/publish` | POST | Fedify gateway | internal | Publish remote-community content | `fedify-gateway/src/routes/internal-publish.ts` | Internal/gateway |
| `/publish-local-community` | POST | Fedify gateway | internal | Publish local-community content | `fedify-gateway/src/routes/internal-publish.ts` | Internal/gateway |
| `/send-local-community-relay` | POST | Fedify gateway | internal | Deliver relay activities | `fedify-gateway/src/routes/internal-delivery.ts` | Internal/gateway |
| `/accept-local-community-follow` | POST | Fedify gateway | internal | Send Accept(Follow) | `fedify-gateway/src/routes/internal-follow.ts` | Internal/gateway |
| `/update` | POST | Fedify gateway | internal | Send Update | `fedify-gateway/src/routes/internal-publish.ts` | Internal/gateway |
| `/delete` | POST | Fedify gateway | internal | Send Delete | `fedify-gateway/src/routes/internal-publish.ts` | Internal/gateway |

**Fluxer ingress:** When the one trusted connector is enabled,
`src/app.py` supplies `src/internal_fluxer_api.py` to `create_http_app()`.
Its authenticated `POST /internal/fluxer/events` endpoint validates an exact
connector-bound DTO under runtime-owned admission and calls only the
`FluxerInboundHandler` SQLite ownership gate. Disabled deployments register
no Fluxer endpoint. The standard Python listener binds `0.0.0.0`, so a
sidecar's shared namespace/loopback delivery does **not** make this endpoint
network-private: bearer authentication is mandatory and nginx must never
route the internal path publicly. The production Node sidecar has not yet
passed live two-container acceptance.

`/healthz` exists in both processes. In the public-host deployment, public `/healthz` should stay gateway-owned unless an operator intentionally adds a Python health alias.

All `/internal/*` routes are authenticated with the shared gateway secret and must not be routed publicly by nginx.

`/actors/`, `/.well-known/webfinger`, `/.well-known/discord-fediverse-bridge/communities`, and `/inbox` are gateway-owned public identity, discovery, or delivery paths and should stay out of the Python bridge namespace. Retired local ActivityPub aliases under `/users/`, `/communities/`, `/c/`, and `/activitypub/` are not public AP gateway surfaces.

## Profile media

`GET /media/profile/{public_id}` resolves an opaque persisted media ID through SQLite before opening canonical bytes beneath the configured media root. It does not mount or enumerate the media directory and returns immutable cache headers plus `nosniff`.

### User Actor outbox

`GET /actors/users/{username}/outbox` returns the canonical collection identity. `?page=N` returns a fixed-size `OrderedCollectionPage`. The private gateway endpoint is `GET /internal/fedify/actors/users/{username}/outbox?page=N&page_size=40`.
