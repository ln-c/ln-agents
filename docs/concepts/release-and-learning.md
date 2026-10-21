# Release and learning

## Release

`R1` approves the exact candidate tree. `M1` then creates a local branch `merge/<run>-<tree prefix>` with the merge commit; it never writes the target branch. A human merges that branch. `MERGED` waits for the merge to land, records the merged commit, and runs `L2`. If the target moved first, `RC` re-runs `E2` and `F1` on the new tip when the changes don't overlap, and stops for a human when they do or when a protected path changed.

## Learning

`L1` is a per-run JSONL file with a SHA-256 hash chain, appended before each model call and fsynced. `L2` appends one `learning` event per merged SHA holding the digest of `gates/L2/<merged_sha>.json`, which records the merged SHA and the patch. It does not refresh an index; none exists yet.

The chain is tamper-evident, not tamper-proof; immutable storage is host configuration.
