# Future tasks

## Isolate User Actor private keys from the Python process

Currently, User Actor private keys are stored in Python-owned persistence and transferred to the Fedify gateway through the authenticated internal actor-identity API. The gateway is the only component that uses those keys for HTTP signing, but Python and its database remain inside the private-key trust boundary.

Future work should move private-key ownership to a gateway-owned signing boundary so Python provides only a non-secret immutable actor or key selector.

Required work:

* introduce durable gateway-owned key storage or an equivalent isolated signing service;
* update registration and key generation so new private keys are written directly to that owner;
* migrate existing User Actor keys with public-key consistency checks and restart-safe progress tracking;
* change gateway actor lookup and signing to load keys directly without receiving them from Python;
* remove private-key fields from the Python-to-gateway identity response;
* define backup, restore, rotation, deletion, rollback, and disaster-recovery behavior for the new key store;
* support synchronized deployment and mixed-version failure handling during migration;
* after verified cutover, remove or securely retire private-key material from Python-owned persistence.

Until this work is completed, new features must not introduce additional private-key consumers or include private-key material in queues, delivery records, snapshots, logs, diagnostics, or request payloads unrelated to the existing gateway identity lookup.

- Restore durable personal-feed publications and comments into Discord after channel loss or rebinding.

## Freeze historical v1 before adding database v2

Before changing current SQLAlchemy models for database v2, pin the complete v1 target schema and required indexes/triggers for the existing v0→v1 step. Historical migration output must no longer depend on current ORM metadata. Add a separate v1→v2 transition and regressions proving both the frozen v0→v1 result and the complete supported upgrade chain preserve data, identity policy and FK guarantees. This is future work; current diagnostics do not introduce v2 or change the migration architecture.
