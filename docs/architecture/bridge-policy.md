# Bridge policy read ownership

`BridgePolicyService` is the only component that reads and merges bootstrap policy with active database rows. Consumers use narrow evaluator methods for one policy question and call `snapshot()` only when one action intentionally reuses the same immutable policy view.

## Action boundaries

| Flow | Action owner | Policy ownership |
| --- | --- | --- |
| Discord command | Command adapter or DiscordOps input | Autocomplete/presentation checks call narrow evaluators; operation inputs own memoized snapshots across preconditions and body execution. |
| Discord event | `DiscordEventRouter` | The router owns the top-level guild admission check. Routed publish and fanout services own their narrower destination/target decisions. |
| ActivityPub event | `dispatch_activitypub_event()` | Dispatch owns inbound origin admission. Selected runtimes and fanout services own later target-specific decisions. |
| Dashboard request | `build_dashboard_data()` | One request-owned snapshot is reused while filtering all rows. |
| Fanout batch | The concrete fanout service | Discord fanout validates each persisted target; federation fanout may reuse one batch snapshot across targets. |

These boundaries do not imply one policy read for an entire command/event. Separate admission, destination, and target checks retain their current reads. Snapshot propagation, action contexts, and read-count reduction belong only to Stage 8 and are not implemented here.

## API rule

Use:

- `BridgePolicyService.is_discord_guild_allowed()` for one guild decision;
- `BridgePolicyService.federation_decision()` for one federation decision;
- `BridgePolicyService.is_super_admin()` for one administrator decision;
- `BridgePolicyService.list_effective_entries()` for one category listing;
- `BridgePolicyService.snapshot()` only when the current action deliberately performs multiple checks against one immutable view.

Lower runtime layers receive the already-composed service dependency. They do not discover, reconstruct, or select another policy source.
