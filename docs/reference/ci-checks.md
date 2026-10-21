# CI checks

`policy/ci.yaml` lists the checks that run at `E2` (acceptance, MC/DC, optional contract, performance and dead-code checks) and `F1` (integration, mutation). Each runs offline in the bwrap sandbox on a copy of the pinned tree with the patch overlaid, with a toolchain built from that tree's `uv.lock`. A required check that is unset is `not_evaluated`, never passed.

- **MC/DC** uses `pymcdc`, which measures only `unittest.TestCase` tests. Pytest-style tests do not count, so `E2` cannot pass on this repository's own code until it has `unittest` tests; such a run routes `E2` → `X` → `H1`.
- **Mutation** uses `mutmut` with a minimum score of 80.
- `scripts/`, `policy/` and `agents/` are protected paths: an implementation patch may not change them.
