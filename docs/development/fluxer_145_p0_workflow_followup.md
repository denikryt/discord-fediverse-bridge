# Plan 145 — P0 workflow follow-up after F2

## Scope

This change builds on the confirmed F2 atomic-ACL-fixture revision (`6f7a3e0`).
It adds **three distinct P0 behavioral scenarios** requested in the VM
verification report and fixes one independently reproduced parent-resolution
bug exposed by the Personal Feed scenario. The delivery state machines,
platform contracts and deployment settings are otherwise unchanged.

## New scenarios

1. **Real `/subscribe-community` submit callback → Follow → Accept → forum post
   and reply** (`test_discord_production_d1_replay.py`). A separate active
   subscription exists first. The target forum cannot publish while pending.
   Two authenticated inbound Accept submissions for the same Follow produce
   one accepted relationship; a post and nested reply publish once each with
   source IDs in the target's thread and no publication for the unrelated actor.
   Only Discord command input and Fedify outbound HTTP are simulated; the
   registered command, application runtime, repositories and callbacks are real.

2. **Remote Personal Feed reply → live delivery worker → anchored discussion
   → Gateway echo → local nested reply** (same module). A remote Create is
   admitted by `dispatch_activitypub_event()` with no discussion thread.
   The worker uses its real durable claim/revision path, while four adapter
   methods fake only Discord SDK I/O. The test waits for the committed delivery
   receipt, asserts one physical thread and message, tests root/thread/message
   ownership against wrong-scope lookups, suppresses the thread echo, and
   requires the local human reply's `inReplyTo` to be the *remote* ActivityPub
   comment, not the root post. The first execution before the fix failed this
   final assertion.

3. **Actual in-flight process death after external Create**
   (`test_platform_delivery_out_of_order_sequences.py`). Target A is already
   delivered. A subprocess running the production fanout coordinator and real
   SQLite claim sends to C. A controlled provider fsyncs an external physical
   Create receipt, then blocks inside its SDK await. Only after that barrier
   does the parent send SIGKILL. Cold Python recovery cannot repost C; even
   after a forced lease expiry it remains unresolved. A retains its proven
   copy. The durable physical-effect file contains exactly one C Create.

## Parent-resolution fix

`PersonalFeedConversationRepository.get_remote_parent_for_discord_reply()`
resolves a confirmed inbound Discord receipt only when **connector**, **root
published object**, **conversation root**, **physical discussion thread**,
**Discord message ID** and **proven physical delivery receipt** all match. The Personal Feed
publisher checks this before falling back to legacy local MessageMapping
lookup. A subsequent remote Update may queue another delivery without
invalidating the already-proven physical reply parent. A bot-rendered remote comment can now be the immediate parent of a
human-authored ActivityPub Create(Note); an unrelated thread or root cannot
become its parent.

## Local verification

- Baseline Personal Feed scenario against unchanged `6f7a3e0`: **FAIL** —
  `inReplyTo` pointed to the root post rather than the remote comment.
- Same scenario after repository/publisher fix: **PASS**.
- Six production Discord replay tests and four fanout/recovery tests:
  **10 PASS** under `unittest` (Discord tests use a temporary SDK import
  stand-in because `discord.py` is unavailable locally).
- Selected Personal Feed runtime, ingress, adapter and persistence tests plus
  the new workflow scenarios: **66 PASS, 2 deselected** using
  `pytest --noconftest` with the same non-committed SDK import stand-in.
  The two existing tests require shared pytest fixtures that cannot load
  without unavailable dependencies in this environment.
- The subprocess-crash test alone passes without an SDK stand-in and uses
  real subprocesses, SIGKILL and SQLite across interpreters.

The import stand-in lives only in the local verification environment; it is
**not** committed, does not model a real Discord Gateway and is not evidence
of an operator deployment. `hypothesis` and `discord.py` were unavailable,
so the frozen full Python suite, Node suites, actual Discord/Fluxer services,
external AP federation and Docker S19 acceptance were **not rerun** on this
revision. Earlier VM acceptance figures apply only to their reported SHAs.

## Still open

The remaining P1/P2 recommendations from `results.md` include inbound AP
out-of-order multi-target recovery, Update/Delete/reconnect sequencing with
an independent model and a two-instance signed HTTP Follow/Accept/Undo test.
A fresh Discord OAuth/registration/user workflow, production networking and
Fedify in-flight drain remain S19 acceptance requirements. These are not
claimed by the three new deterministic P0 scenarios.
