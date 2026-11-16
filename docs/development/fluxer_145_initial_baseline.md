# Issue #12 / plan 145 — initial test baseline (CP01)

Git starting point: `feature/fluxer-support` `2fd0fdd2f06f2781bb5a5cf293ba26ddbd7ba5dc` (plan F1–F6). At this snapshot, the local `main` is 45 commits ahead of the feature branch; the feature branch is 136 commits ahead of local `main`. No source code has been changed as part of this baseline.

Full-suite commands were attempted **before production changes** on 2026-11-16:

| Suite | Command | Result | Classification |
| --- | --- | --- | --- |
| Python | `uv run --frozen --extra dev pytest -q` | **NOT RUN**: `uv` could not resolve/download `sortedcontainers`, a dependency of `hypothesis`, because this container cannot resolve `files.pythonhosted.org` | Environment/dependency blocker, not a test failure |
| Fluxer Node | `npm run check:lock && npm run check && npm test` in `fluxer-connector` | `check:lock` passed; `tsc` exited 2: `@types/node` and SDK package dependencies are absent | Environment/dependency blocker; tests not reached |
| Fedify Node | `npm run check && npm test` in `fedify-gateway` | `tsc` exited 2: `@types/node`, `@fedify/fedify`, `@fedify/vocab-runtime` dependencies are absent | Environment/dependency blocker; tests not reached |

Installed system Python lacks `discord.py` and `hypothesis`; `fluxer-connector/node_modules` and `fedify-gateway/node_modules` are absent. This is **not** evidence that the original code passes or fails these suites. Restore locked dependencies and rerun all suites before declaring #12 complete.

CP01 test-first contract (`python -m unittest discover -s tests/behavior -p test_single_platform_runtime.py -v`) produces **one expected error, three expected failed subcases and three runtime skips**: production `Settings` has no `bridge_platform` and accepts invalid/mixed mode flags. Runtime tests skip without `discord.py`. These red results must not be reclassified as previously existing failures once CP02–CP06 is implemented.

Critical invariants being implemented: single exclusive connector; read-only pre-migration DB/AP signing-key protection; no Discord SDK/OAuth construction in Fluxer mode; SDK-owning Node RPC gap + live bot identity gate; verified exact physical writes; explicit direct-transport vs durable SQLite retry authority; observation-only Gateway; preserving Discord AP IDs/Personal Feed/mutations; isolated Compose credentials and independent origins. Each positive/negative scenario belongs to the appropriate checkpoint rather than silently enabling future-scope features.
