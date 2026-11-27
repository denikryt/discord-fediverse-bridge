# CP36 — CP35 baseline and evidence limits

Source: `fa95013607e5aab86ba0618d4b858a8825c3165e` (clean Git checkout), Python 3.13.5, Node 22.16.0, npm 10.9.2. Development baseline only; no live instance was accessed.

* Standard `python -m pytest -q` stopped in `tests/conftest.py` with `ModuleNotFoundError: hypothesis`; its full per-ID baseline is unavailable in this sandbox. An isolated execution of existing `tests/test_remote_community_group_identity_constraints.py` with `--noconftest` yielded 5 passed, 2 failed on old `ready_v1` assertions against CP35 `ready_v2`. These failures were not modified.
* `uv pip install -e '.[dev]'` could not obtain build dependencies (DNS/network unavailable). `npm ci --offline` failed because the npm cache lacks `yaml-2.8.3.tgz`, so Gateway compilation and its 59 scripts could not be run locally. Python `discord.py` is absent.
* The supplied CP35 field report gives **1217 passed, 215 failed, 6 errors, 1 xfailed**, DiscordOps **50 passed**, Gateway **53 passed, 6 failed**. These are independent source-reported observations, **not locally recreated per-ID results**.
* Pilot evidence identifies `/post/79` as an unmapped Community comment Delete after Python ingress, with a `post.deleted` receipt; direct Group Update/Delete and a source-starter edit were separately observed failing. Raw authenticated NodeBB wire envelopes, the exact normalized `/post/79` object, verified signature metadata and full per-ID CP35 logs are not contained in the supplied `results.md`. They must not be fabricated.
* The new CP36 behavioral fixtures use isolated artificial `nodebb.example` identifiers exclusively to expose SQLite claim/tombstone boundaries. They are not asserted to be the captured NodeBB packet. The new tombstone and fingerprint tests are expected to fail until CP38/CP39 introduce durable Group state and transactional receipts. The lost-ACK no-duplicate-POST claim test passes already on CP35.

Security: No private keys, personal account identifiers, webhook secrets, Discord tokens or private DB copies are checked in.
