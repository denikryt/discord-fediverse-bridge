# Plan 145 — F3 accepted Create crash barrier

## Defect and test sensitivity

The controlled P0 provider opened its `*.physical.jsonl` acceptance file before
writing and fsyncing the physical Create record. Parent-side `Path.exists()`
therefore permitted SIGKILL before any accepted physical effect had been
recorded. An explicit pause after opening but before writing reproduced the
failure: the acceptance path was already present, although the record was
missing. This is a test synchronization error, not evidence of production
Create redelivery.

## Correction

`tests/behavior/test_platform_delivery_out_of_order_sequences.py` retains the
production coordinator, SQLite claim and separate POSIX child process. The
provider now opens a **sibling temporary file**, pauses at a parent-controlled
prepublication barrier, writes one JSON record, flushes and fsyncs that file,
closes it, and publishes it using atomic `os.replace()`. Before releasing the
provider, the parent verifies the final path does not exist and the child is
alive. After publication it parses one complete record, confirms the child is
still alive and SIGKILLs it before any SDK ACK. All existing assertions for
A/C target states, attempt-key correlation, cold recovery, expired lease and
no duplicate physical Create remain in force.

No production code, lockfiles or platform contracts are changed. The temporary
marker files are cleaned up even if the test fails.

## Local verification

- Red-phase regression: the original active-file writer failed with
  `an incomplete physical record was published` while paused before write.
- Green-phase targeted accepted-Create SIGKILL test: passed, including the
  controlled prepublication pause and cold SQLite recovery assertions.
- `python -m pytest --noconftest -q tests/behavior/test_platform_delivery_out_of_order_sequences.py`:
  4 passed.
- Adjacent delivery/order/recovery/mutation tests: 26 passed, 6 subtests.
- Individual repeat runs of the SIGKILL scenario also passed. A separate
  bundled multi-repeat command exceeded its execution limit; this was not
  counted as a passing run.

The complete frozen Python/Node test suites, Docker and operator/live Fluxer
acceptance were not rerun here; earlier results belong to their recorded SHAs.
The S19 acceptance gate remains open.
