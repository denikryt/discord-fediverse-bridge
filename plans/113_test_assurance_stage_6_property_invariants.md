# 113 — Test Assurance Stage 6: Property-Based Policy Invariants

## Problem / Goal

The two typed contract pilots protect named ban and bridge-policy rules, but example cases cannot economically explore the value space of domains, URLs, entry ordering, duplicate policy rows, and malformed identifiers. Stage 6 introduces Hypothesis only at pure or narrow policy boundaries where independent invariants are simple, fast, and diagnostically useful.

## Scope and Boundary

### Changes

- Add Hypothesis as a development dependency.
- Regenerate `uv.lock` from public PyPI so the dependency graph contains no environment-specific internal registry URLs.
- Register documented deterministic development and CI Hypothesis profiles.
- Add property tests for bridge-policy normalization and immutable snapshot semantics.
- Record runtime and reproducibility commands in test-assurance documentation.

### Preserves

- Existing named scenario and typed contract cases remain the primary product narratives.
- Expected results are declared as simple invariants, never by calling a production evaluator as an oracle.
- Production code and public APIs remain unchanged unless a generated minimized example proves a defect.

### Explicitly excluded

- Stateful action sequences (Stage 7).
- Pairwise/3-way model generation and cross-entry-point adapters (Stage 8).
- Runtime-wide arbitrary state generation.
- Concurrency, fault injection, or mutation testing.
- Project-wide conversion to Hypothesis.

## Selected Invariants

Create `tests/property/test_bridge_policy_properties.py` and a focused strategy module only if the tests become unreadable without one.

The stage must cover these independent properties:

1. **Block precedence** — for any generated valid host, placing that host in the federation block set denies it regardless of whether it also appears in the allow set.
2. **Unrelated-entry stability** — adding a distinct unrelated allow/block entry does not change the decision for a target host when the target’s own relevant policy membership is unchanged.
3. **Order and duplicate invariance** — reordering or duplicating effective entries does not change federation and guild decisions.
4. **Canonical host equivalence** — case changes, an optional trailing dot, and URL wrapping preserve the same normalized DNS host for generated ASCII domain labels.
5. **Malformed host rejection** — generated values containing whitespace or structurally empty labels fail normalization and produce `INVALID_HOST` decisions.
6. **Discord identifier invariants** — generated decimal snowflakes normalize unchanged; non-decimal/whitespace-bearing identifiers are rejected and cannot become allowed guilds or super-admins.

The strategies must generate bounded readable examples and avoid arbitrary Unicode or full URL grammars unless the product contract explicitly supports them.

## Independent Oracle Rules

Expected outcomes must be expressed directly:

```python
snapshot = BridgePolicySnapshot(entries)
assert snapshot.federation_decision(host).allowed is False
```

for block precedence, or by equality between two differently ordered snapshots where both are also checked against an explicit expected result in the base case.

No property may compute `expected` by calling `BridgePolicyService`, `BridgePolicySnapshot`, or another production policy helper and then compare the action under test to that value.

## Hypothesis Profiles

Register profiles in `tests/conftest.py`:

- `dev`: moderate examples, no deadline, deterministic database disabled because tests are pure;
- `ci`: larger example count, no deadline, health checks kept unless one is specifically and narrowly justified;
- profile selection through standard `HYPOTHESIS_PROFILE`, defaulting to `dev`.

Failure output must retain Hypothesis’s minimized example and reproduction blob where available.

## Dependency and Lockfile Contract

Update `pyproject.toml` dev extras with a compatible Hypothesis requirement. Regenerate `uv.lock` using `https://pypi.org/simple` as the default index and verify that neither `applied-caas-gateway` nor `internal.api.openai.org` appears anywhere in the committed lockfile.

## TDD and Implementation Steps

1. Add the dependency and profiles.
2. Write the block-precedence property and run it against current production code.
3. Add unrelated-entry and order/duplicate invariants.
4. Add canonical-equivalence and malformed-host properties.
5. Add Discord identifier properties.
6. Run the focused property suite under both `dev` and `ci` profiles.
7. Measure focused runtime and document cadence.
8. Run all existing Python groups, report commands, compile/diff checks, and full gateway checks.

## Touched Files

- `pyproject.toml`
- `uv.lock`
- `tests/conftest.py`
- `docs/development/test-assurance.md`

## New Files

- `plans/113_test_assurance_stage_6_property_invariants.md`
- `tests/property/test_bridge_policy_properties.py`
- `tests/property/__init__.py` only if required for collection/import clarity.

## Tests and Exit State

Focused commands:

```bash
HYPOTHESIS_PROFILE=dev .venv/bin/python -m pytest -q tests/property
HYPOTHESIS_PROFILE=ci .venv/bin/python -m pytest -q tests/property
```

Stage 6 is complete only when:

- all selected invariants pass with minimized, readable generated values;
- no production evaluator is reused as its own oracle;
- Hypothesis remains confined to the focused property layer;
- public-PyPI lockfile verification passes;
- full repository tests and checks pass;
- the stage plan is committed and a complete verified bundle is produced.

## Handoff

Stage 7 may reuse Hypothesis configuration and bounded strategies, but it must add a separate state machine with independent model state. It must not reinterpret these value-space properties as lifecycle coverage.
