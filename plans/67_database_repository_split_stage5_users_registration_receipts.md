# 67 — Database repository split Stage 5: users, registration, and event receipts

## Problem / Goal

Stage 5 extracts service-support persistence from `Database`: registered user identity rows, browser/OAuth registration session rows, and inbound ActivityPub event receipt/idempotency rows. The goal is to move these relatively self-contained domains into explicit repositories while preserving current HTTP route and inbound handler behavior.

## Expected Behavior

Runtime behavior is unchanged.

Registration sessions, OAuth state updates, completed user linkage, user lookups, dashboard user listing, and ActivityPub event receipt idempotency must keep the same row writes, lookup predicates, status transitions, and error behavior. Existing `Database.*` methods remain temporary forwarding wrappers until Stage 8.

## Stage Boundary

Owns extraction for:

- `User` persistence into `UserRepository`.
- `RegistrationSession` persistence into `RegistrationSessionRepository`.
- `ActivityPubEventReceipt` persistence into `EventReceiptRepository`.

Does not own subscription behavior, local-community behavior, ActivityPub JSON rendering, object mapping, remote actor cache, legacy Lemmy mappings, Discord fanout groups, schema changes, or public FastAPI route semantics.

## Architecture

`Database` remains the single owner of engine/session/create_all/migrate. Repositories share Database session ownership and do not create independent engines or session factories.

Add three repository modules under `src/db/repositories/` and instantiate them from `Database.__init__()` using the existing shared session provider:

```python
self.users = UserRepository(self.session)
self.registration_sessions = RegistrationSessionRepository(self.session)
self.event_receipts = EventReceiptRepository(self.session)
```

The current `Database.create_user(...)`, `Database.get_registration_session_by_token(...)`, and `Database.create_event_receipt(...)` style methods become temporary wrappers. Stage 8 removes those wrappers after call sites move to repository properties.

## Touched Files

- `plans/67_database_repository_split_stage5_users_registration_receipts.md`
- `src/db/database.py`
- `src/db/repositories/__init__.py`
- `src/db/repositories/users.py`
- `src/db/repositories/registration_sessions.py`
- `src/db/repositories/event_receipts.py`
- `docs/development/navigation.md`

## New Files

- `src/db/repositories/users.py`
- `src/db/repositories/registration_sessions.py`
- `src/db/repositories/event_receipts.py`

## Implementation Steps

1. Move user method bodies from `Database` into `UserRepository` without changing row fields or lookup predicates.
2. Move registration session method bodies into `RegistrationSessionRepository` without changing OAuth state, Discord identity, completion, expiry, or missing-token behavior.
3. Move event receipt method bodies into `EventReceiptRepository` without changing delivery-id lookup or missing receipt error behavior.
4. Import and instantiate the three repositories in `Database` using the shared session provider.
5. Replace the extracted `Database` methods with temporary forwarding wrappers preserving current signatures and return annotations.
6. Update navigation docs so registration and idempotency maintainers can find the new repository files.
7. Run focused registration/idempotency tests and then full pytest.

## Tests

Focused Stage 5 command:

```bash
./.venv/bin/pytest -q \
  tests/test_registration_flow.py \
  tests/behavior/test_registration_scenarios.py \
  tests/behavior/test_inbound_scenarios.py \
  tests/test_end_to_end_dedup_flow.py
```

Full suite:

```bash
./.venv/bin/pytest -q
```

## Regression / Blind-Spot Analysis

The main registration risk is changing token lookup, OAuth state expiry, or completed-session user linkage. Method bodies should move mechanically.

The main event receipt risk is changing idempotency keys or missing receipt error handling. Delivery IDs and status updates must remain unchanged.

The session ownership risk remains the same as prior stages: repositories must receive `Database.session` and must not create their own engine/session factory.

Wrappers are temporary extraction scaffolding only and are removed by Stage 8.

## Open Questions

None blocking. This stage intentionally keeps call sites on existing `Database.*` methods to isolate the extraction from public route behavior.
