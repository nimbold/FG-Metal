# Z9R-H2-B — Read-only storage admission

## Measurement window

The read-only live measurement ran on 2026-10-14 20:04:55–20:04:56 UTC (2026-10-15 00:04:55–00:04:56 Asia/Baku). It used `storage_policy.disk_free_bytes()`, `measure_project_usage()`, `conservative_project_usage_bytes()`, `admission_uncertainty_margin_bytes()`, and the pure `projected_storage_admission()` calculation. No Wine, Finder, or other application was launched. No repository files were written by this check; this evidence is stored at `/tmp/Z9R-H2-B-STORAGE.md` pending any later report append and Gate 0 refresh.

## Current inventory and attested reading

- Canonical inventory: `/Users/nima/Documents/Code/FG-Metal/experiments/storage/storage-inventory.json`; generated 2026-10-14 19:26:46.967706 UTC. Current SHA-256 `923b92c730b92f7d8b130d295ed0d9f08a6e831803bf2ff80315897ee2e98119` matches the `inventory_sha256` bound by `/Users/nima/Documents/Code/FG-Metal/experiments/storage/storage-gate0-attestation.json`.
- Stored Gate 0: mode `final`, status `PASS`, signature verified, created 2026-10-14 19:26:51.590472 UTC (age 38m04.7s at the live measurement; within the policy's one-hour age limit).
- Inventory snapshot: free `70,465,572,864` B; scoped allocated inode bytes `49,957,994,496` B.
- Live policy read: free `70,456,410,112` B (`65.617645 GiB`); project logical bytes `94,987,150,165`; live hard-link-deduplicated allocated inode bytes `49,962,659,840` B (`46.531353 GiB`). No optional-root disappearance was reported. The policy takes the maximum of live allocation plus any inventory unlinked-mount upper bound and the inventory scope; here the live reading is larger, and the inventory upper bound is zero.
- The live free-space reading is `9,162,752` B below the inventory snapshot, well inside the policy's `2 GiB` Gate 0 free-space drift tolerance. The live total scope is `4,661,248` B above the Gate 0 full-scope measurement (`49,957,998,592` B). This report records the stored attestation as valid and fresh but does not claim that this bounded check independently completed the full live Gate 0 blocker evaluation. Complete Gate 0 must be rechecked after any repository report append before lifecycle authorization.

## Future-only reservation calculation

The scenario is one future managed run: the standard `3 GiB` PrefixLease plus the requested persistent output reservation of `4,974,444,544` B. The output reservation is not treated as storage already allocated today.

- Maximum new disk estimate: `3,221,225,472 + 4,974,444,544 = 8,195,670,016` B.
- Uncertainty margin: `max(256 MiB, ceil(10% × 8,195,670,016)) = 819,567,002` B.
- Projected peak addition: `9,015,237,018` B.
- Projected free: `70,456,410,112 − 9,015,237,018 = 61,441,173,094` B (`57.221552 GiB`).
- Projected scoped FG-Metal usage: `49,962,659,840 + 9,015,237,018 = 58,977,896,858` B (`54.927447 GiB`).

Policy limits are projected free at least `50 GiB` and projected scope at most `55 GiB`. Both numerical limits pass; the projected scope has `77,903,462` B of headroom under the 55 GiB ceiling. Policy advisories apply because projected free is below the `60 GiB` preferred target and projected scope exceeds the `52 GiB` warning threshold. The pure numerical admission result is `admitted=true`, with no blockers and those two warnings.

## Historical closure accounting

No historical-output credit or subtraction was applied. The schema-v3 correction record for `step11d3b-baseline-01-20261014-130823-44892b9b` explicitly has `counts_toward_project_scope=true`, `authorizes_execution=false`, and `changes_original_gate_result=false`. Its still-present run tree measured `725,688,320` allocated bytes (16 regular files; `720,231,108` logical bytes) during this read. The live scoped scan includes this tree.

## Decision

**Numerical storage admission: GO with the two advisories above** for the stated future-only reservation. **Lifecycle authorization: not established by this storage report.** The signed final Gate 0 artifact is fresh and valid at the recorded timestamp, but a later repository append requires Gate 0 to be refreshed and fully revalidated against the resulting live state before lifecycle authorization.

## Sources

- `/Users/nima/Documents/Code/FG-Metal/experiments/storage/storage_policy.py` — constants, 10%/256 MiB uncertainty rule, conservative scope, admission thresholds.
- `/Users/nima/Documents/Code/FG-Metal/experiments/storage/storage_inventory.py` — bounded no-follow allocated-inode inventory and APFS clone-awareness note.
- `/Users/nima/Documents/Code/FG-Metal/experiments/storage/storage-inventory.json` and `/Users/nima/Documents/Code/FG-Metal/experiments/storage/storage-gate0-attestation.json` — current canonical snapshot and signature-bound measurements.
- `/Users/nima/Documents/Code/FG-Metal/experiments/storage/historical-terminal-closures/step11d3b-baseline-01-20261014-130823-44892b9b.correction-1.json` and its run tree — historical failure remains charged in scope.
