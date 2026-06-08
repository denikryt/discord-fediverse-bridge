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
| `/healthz` | GET | Fedify gateway | public | Gateway health | `fedify-gateway/src/server.ts` | Gateway |
| `/.well-known/webfinger` | GET | Fedify gateway | public | Actor discovery | `fedify-gateway/src/server.ts` | Gateway |
| `/.well-known/discord-fediverse-bridge/communities` | GET | Fedify gateway | public | Bridge-owned public local-community discovery | `fedify-gateway/src/server.ts` | Gateway |
| `/inbox` | POST | Fedify gateway | public AP | Inbound ActivityPub | `fedify-gateway/src/federation.ts` | Gateway |
| `/users/:username` | GET | Fedify gateway | public AP | Local user actor | `fedify-gateway/src/server.ts` | Gateway |
| `/communities/:slug` | GET | Fedify gateway | public AP | Local community actor | `fedify-gateway/src/server.ts` | Gateway |
| `/c/:slug` | GET | Fedify gateway | public AP | Lemmy-style community alias | `fedify-gateway/src/server.ts` | Gateway |
| `/communities/:slug/outbox` | GET | Fedify gateway | public AP | Community outbox | `fedify-gateway/src/server.ts` | Gateway |
| `/communities/:slug/followers` | GET | Fedify gateway | public AP | Community followers collection | `fedify-gateway/src/server.ts` | Gateway |
| `/users/:username/outbox` | GET | Fedify gateway | public AP | User outbox | `fedify-gateway/src/server.ts` | Gateway |
| `/users/:username/followers` | GET | Fedify gateway | public AP | User followers collection | `fedify-gateway/src/server.ts` | Gateway |
| `/follow-community` | POST | Fedify gateway | internal | Send Follow | `fedify-gateway/src/server.ts` | Internal/gateway |
| `/unfollow-community` | POST | Fedify gateway | internal | Send Undo(Follow) | `fedify-gateway/src/server.ts` | Internal/gateway |
| `/publish` | POST | Fedify gateway | internal | Publish remote-community content | `fedify-gateway/src/server.ts` | Internal/gateway |
| `/publish-local-community` | POST | Fedify gateway | internal | Publish local-community content | `fedify-gateway/src/server.ts` | Internal/gateway |
| `/send-local-community-relay` | POST | Fedify gateway | internal | Deliver relay activities | `fedify-gateway/src/server.ts` | Internal/gateway |
| `/accept-local-community-follow` | POST | Fedify gateway | internal | Send Accept(Follow) | `fedify-gateway/src/server.ts` | Internal/gateway |
| `/update` | POST | Fedify gateway | internal | Send Update | `fedify-gateway/src/server.ts` | Internal/gateway |
| `/delete` | POST | Fedify gateway | internal | Send Delete | `fedify-gateway/src/server.ts` | Internal/gateway |

`/healthz` exists in both processes. In the public-host deployment, public `/healthz` should stay gateway-owned unless an operator intentionally adds a Python health alias.

All `/internal/*` routes are authenticated with the shared gateway secret and must not be routed publicly by nginx.

`/users/`, `/communities/`, `/c/`, `/.well-known/webfinger`, `/.well-known/discord-fediverse-bridge/communities`, and `/inbox` are gateway-owned public identity, discovery, or delivery paths and should stay out of the Python bridge namespace.
