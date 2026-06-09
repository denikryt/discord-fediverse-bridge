# 116 — Test Assurance Stage 9: Failure Ordering and Deterministic Concurrency

## Problem / Goal

The bridge already has strong happy-path, policy, lifecycle, and interaction coverage, but local-community federation relay still lacks a compact set of deterministic tests that prove failure ordering and action-scoped consistency at its persistence/network boundary. The goal is to add observable behavior tests for fail-closed policy reads, per-target partial failure with retry, and policy changes during an in-flight fanout without expanding into mutation testing or broad concurrency infrastructure.

## Expected Behavior

1. If the effective federation policy cannot be read before relay setup, the relay action raises the repository error and performs no relay source, delivery, or gateway side effects.
2. If one gateway outcome fails while another succeeds, the action records delivered and failed target states independently, returns an accurate summary, and a retry attempts only the failed target.
3. If policy changes after one relay action has taken its snapshot but before gateway completion, the in-flight action keeps its original target set; the next relay action observes the new policy. This is deterministic action-scoped visibility, not eventual or sleep-based timing.
4. Existing audit atomicity tests remain the authoritative coverage for state mutation followed by audit failure; this stage does not duplicate or redesign that transaction contract.

## Architecture

Add behavior scenarios to `tests/behavior/test_local_community_remote_fanout_scenarios.py`, reusing its real `LocalCommunityRuntime`, SQLite repositories, remote-subscriber rows, actual relay renderer, and fake gateway outer boundary.

Use only outer-boundary fault controls:

- monkeypatch `BridgePolicyService.snapshot` to raise before persistence for the policy-read failure;
- return mixed `SendLocalCommunityRelayOutcome` values from the gateway for partial failure;
- use `asyncio.Event` barriers inside the fake gateway to pause one in-flight relay after snapshot/target selection, mutate the real bridge-policy repository, then release the call.

No production method is mocked merely to force internal branches. Assertions inspect durable relay source/delivery rows and gateway payloads.

### Example partial-failure flow

```text
followers: alice, carol
first gateway result: alice=delivered, carol=failed
first summary: attempted=2, delivered=1, failed=1
retry gateway request: carol only
final durable state: alice=delivered, carol=delivered
```

### Example policy-change flow

```text
action A reads snapshot with both targets allowed
A blocks in fake gateway
repository adds federation_block for lemmy.example
A completes with its original target set
new action B reads the new snapshot and sends to zero lemmy.example targets
```

## Touched Files

- tests/behavior/test_local_community_remote_fanout_scenarios.py
- docs/testing.md

## New Files

- plans/116_test_assurance_stage_9_failure_ordering.md

## Implementation Steps

1. Add a failing behavior test for policy snapshot failure before relay persistence and transport.
2. Add a failing behavior test for mixed gateway outcomes and failed-target-only retry.
3. Add a deterministic barrier-based behavior test for policy mutation during an in-flight relay action.
4. Implement only test-support adjustments required to expose durable delivery states; do not change production behavior unless a test demonstrates a confirmed defect.
5. If a confirmed production defect appears, record it in `notes/known_issues.md`, keep the failing regression test, and make only the smallest in-scope correction.
6. Update testing documentation with the Stage 9 targeted command and the exact contracts proved.
7. Run focused tests, all Python test groups, compile/diff checks, and the gateway checks.

## Tests

Focused command:

```bash
python -m pytest -q tests/behavior/test_local_community_remote_fanout_scenarios.py
```

Assertions must cover:

- policy failure raises and leaves zero source/delivery rows and zero gateway calls;
- partial outcomes persist independently;
- retry excludes already delivered targets;
- summaries match attempted/delivered/failed counts;
- in-flight action target set is stable after snapshot;
- next action observes the changed policy;
- no sleeps or timing assumptions are used.

Full validation follows the project’s existing test-assurance groups plus `compileall`, `git diff --check`, and `fedify-gateway/npm test`.

## Regression and Blind-Spot Analysis

- A gateway exception before outcomes is distinct from mixed per-target outcomes; this stage covers the latter because durable retry behavior is defined per delivery row. Whole-call transport exceptions remain existing runtime error behavior unless a failing scenario proves an undefined persistence result.
- The barrier test proves snapshot visibility only for local-community federation relay; it does not claim all fanout implementations share that contract.
- SQLite process-level locking and broad concurrent ban races are intentionally excluded because their expected product conflict policy is not defined by current documentation.
- Existing audit rollback tests already prove atomic mutation/audit behavior and must remain green.

## Handoff

This stage changes only assurance coverage and documentation unless a regression test proves a production defect. It preserves all Stage 1–8 contract models, reports, property/stateful layers, and pairwise behavior. Stage 10 may rely on these deterministic failure tests when selecting mutation targets, but Stage 10 is not implemented here.

## Open Questions

None. The selected outcomes are defined by current relay persistence behavior, action-scoped policy snapshots, and existing audit transaction tests.
