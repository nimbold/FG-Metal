# Z9R-H1-B — Gate 0 blocker semantics

**Scope:** read-only source and saved-record review. No code, evidence, or storage state was changed; no tests or experiments were run.

## Source path: discovery → accounting → preflight

1. **Historical discovery.** `storage_policy._signed_persistent_output_snapshot()` opens `experiments/storage/manifests` with `_open_directory_nofollow`, enumerates every filename ending in `.json`, and sorts the names (`experiments/storage/storage_policy.py:574-598`). It has no age, run-result, terminal-status, or retirement filter.

2. **Manifest validation.** Each candidate is read through the open directory descriptor and checked for stable `lstat` identity, owner, regular-file type, link count, mode, and size (`storage_policy.py:599-625`). It is accepted for accounting only if its ID and filename agree, its path is canonical, required fields are present, `decision == "ALLOW"`, bytes are canonical JSON, and the HMAC verifies (`storage_policy.py:627-645`). `_validated_manifest_estimates()` requires an integer `estimated_persistent_bytes` and internally consistent reservation totals (`storage_policy.py:1219-1266`). The required-field set contains admission and storage fields, but no run completion or result classification field (`storage_policy.py:63-79`). The HMAC is over canonical manifest data (`storage_policy.py:1180-1195`).

3. **Signed-output accounting.** For each accepted manifest, the code snapshots the exact output tree, allocated bytes, inode metadata, and tree digest. If the measured tree is over the original persistent reservation, the function emits `signed experiment output exceeds its persistent reservation: <experiment_id>` and skips adding that run to `bindings` and `output_paths` (`storage_policy.py:656-693`). A path is eligible for exclusion from the unrelated-growth comparison only after it passes this check. It remains in the full project usage either way: `measure_project_usage()` sums all discovered project inodes and supplies a separate excluded-path subtotal only for comparison (`storage_policy.py:2106-2206`). The focused test confirms both behaviors: over-reservation adds a blocker and excludes neither output nor manifest; within-reservation output remains in whole-project usage (`test_storage_policy.py:424-453`).

4. **Blocker propagation and repeat checks.** `_gate0_blockers()` calls the snapshot, appends its blockers, compares the resulting binding rows with Gate 0's stored `signed_persistent_output_bindings`, and emits `signed historical experiment outputs changed since Gate 0 certification` when a prior row differs (`storage_policy.py:1769-1786, 1969-1981`). It takes a second snapshot after other measurements and blocks if paths or snapshot identity changed during the check (`storage_policy.py:2052-2060`). The run-output identity test demonstrates that a changed historical row blocks, while a newly reserved output bound to the current Gate 0 does not count as a changed frozen historical row (`test_g0c_checks.py:191-209`).

5. **Gate 0 preflight path.** `storage_gate0._refresh("preflight")` enters `_refresh_locked()` under the storage-certification guard (`experiments/storage/storage_gate0.py:1037-1051`). After reconciliation and inventory creation, `_write_exact_live_storage_baseline()` takes a signed-output snapshot and raises immediately if it has any blocker (`storage_gate0.py:114-128`). `_write_baseline_or_fail_closed()` records that failure in a signed `BLOCKED` attestation (`storage_gate0.py:203-218`); `main()` prints `Gate 0 refresh blocked` and returns failure (`storage_gate0.py:1374-1388`). If baseline creation succeeds, preflight also calls `_gate0_blockers()` on read-back and durably marks a disagreeing attestation blocked (`storage_gate0.py:1356-1365`).

   The current saved `experiments/storage/storage-gate0-attestation.json` records the exact baseline failure at lines 44-45: `signed experiment output accounting is blocked: signed experiment output exceeds its persistent reservation: step11d3b-baseline-01-20261014-130823-44892b9b`. The original signed manifest names that run, has `decision: ALLOW`, and fixes `estimated_persistent_bytes` at 314,572,800 (`experiments/storage/manifests/step11d3b-baseline-01-20261014-130823-44892b9b.json:44-67`). The saved BR9R2 report records its Gate POST tree at 598,573,056 bytes and later tree at 725,688,320 bytes against that same reservation (`experiments/dxvk_macos_step11d3b/STEP-11D3-BR9R2-REPORT.md:906-931`).

## Answers

### 1. Which exact code path emits the blocker?

`experiments/storage/storage_policy.py::_signed_persistent_output_snapshot()` at the `output_allocated > estimates["estimated_persistent_bytes"]` condition (lines 677-680). The exact blocker reaches today's preflight through `experiments/storage/storage_gate0.py::_write_exact_live_storage_baseline()` (lines 123-128), then `_write_baseline_or_fail_closed()` signs the attestation as `BLOCKED` (lines 212-218). `_gate0_blockers()` independently reuses the same blocker path during normal admission and Gate 0 read-back (storage policy lines 1969-1981; Gate 0 lines 1359-1365).

### 2. Does Gate 0 intentionally require every historical run forever to remain within its original reservation?

The source contains no explicit “forever” policy statement, but its effective rule is yes for every retained, authenticated `ALLOW` manifest in the manifest directory. Every snapshot scans all `.json` manifests with no terminal, age, or archival exception; a signed output above its original reservation blocks baseline creation. Removing the check would require changing or removing the manifest/output evidence or changing this policy path. The current code has no supported retirement transition for run-output manifests.

This is specifically an admission/accounting rule for accepted signed manifests. Invalid, refused, malformed, or unauthenticated candidates are not treated as authorized signed-output exclusions; they are not an alternate retirement mechanism.

### 3. Does an existing terminal, retired, or archive state already exist?

No run-level terminal or retired state exists in the signed-output manifest/accounting schema inspected. There are adjacent mechanisms with narrower scope:

- `PREFIX_RETIRED_MARKERS_ROOT` and `recover_interrupted_storage_smoke_lease_marker()` retire a stale **storage-smoke PrefixLease marker** (`storage_policy.py:88-100, 2825-2890, 3053-3074`). Its recovery receipt is explicitly `CURRENT_STATE_STORAGE_RECOVERY`, with `historical_cleanup_certified: false`; its statement says current-state reconciliation does not certify historical cleanup (`storage_policy.py:2992-3040`).
- `build_retention.status_snapshot()` and `capacity_blockers()` expose active, completed-pending-retirement, and failed-or-retiring **build roots** (`experiments/storage/build_retention.py:3448-3467, 3507-3518`); Gate 0 consumes those build counts separately (`storage_policy.py:1873-1899`). This accounting applies to build roots and leases, not experiment run-output trees.

Neither mechanism is a closure for a failed graphics run or an over-reservation signed output. Reusing the PrefixLease recovery receipt would assert less than the required historical closure and would not make the output accounting accept the historical exception.

### 4. Are failed runs distinguished from active or reusable runs?

Not by signed-output accounting. The signed manifest's `decision: ALLOW` means storage admission; the snapshot does not read the run's `run-result.json`, R5/T8 classification, or cleanup receipt. Thus a run that later records `status: FAIL` remains an admitted signed manifest for this check. Active PrefixLease markers are checked separately by `_gate0_blockers()` (`storage_policy.py:1854-1867`) and current-run binding/receipt validation is separate (`storage_policy.py:1523-1582, 1943-1968`). A run's terminal failure is not currently a Gate 0 state transition.

### 5. Is this blocker protecting current state, historical integrity, or both?

Both. It protects historical integrity by refusing to attest an output tree as a valid reservation-limited signed binding when its measured allocation exceeds the manifest's immutable reservation. It protects current-state accounting by keeping the overrun from being silently rebased as an excluded, stable run output. The whole-project inode total continues to include those bytes; only the comparison subtotal excludes a fully bound output. The existing tests explicitly verify that an overrun is not excluded (`test_storage_policy.py:444-453`) and that historical binding changes fail Gate 0 (`test_g0c_checks.py:191-199`).

Therefore the blocker is not evidence that the 55 GiB project scope or 50 GiB free-space thresholds were crossed. It is an independent integrity/accounting stop, and a closure must preserve that distinction while leaving the physical bytes in scope.

### 6. What is the smallest architecture-compatible way to close a known terminal historical failure?

Add a separate append-only, authenticated **per-run terminal-closure record** that Gate 0 can verify; keep the original manifest, Gate POST, cleanup receipt, run result, and R5/T8 untouched. Integrate it narrowly into `_signed_persistent_output_snapshot()` for an explicitly identified run, with the original manifest hash and reservation, the exact recorded Gate POST failure, a terminal/no-active-process-and-lease proof, and a measured current output-tree identity/digest/allocation snapshot. The closure should be non-authorizing and must not imply that the run stayed within its old reservation.

For that one validated closure, Gate 0 could accept the measured overrun as known historical evidence and return a full identity binding for the run. It must continue to count the manifest, closure record, and output bytes in whole-project inventory and 50/55 admission calculations. Each later Gate check must compare the current tree to the closure snapshot and fail on any unexplained tree change; the existing binding comparison provides the closest current mechanism for that check. All other signed-output overruns, active leases, and nonlease mutations should continue to block. New runs must still require a fresh Gate 0 attestation through the existing manifest/runner admission path; the closure itself must never authorize execution.

This is a design conclusion from the source trace, not an implementation or proof that the required liveness, process, lease, identity, and filesystem-race checks are already available in one existing API.

## Uncertainty and limits

- This report traces the checked-in/current working-tree source and the saved current Gate 0 attestation. It does not rerun preflight, tests, or storage measurement.
- `_signed_persistent_output_snapshot()` silently skips candidates that fail portions of manifest parsing/validation; this report makes no claim that malformed-manifest handling is a suitable lifecycle state.
- The user-supplied historical run facts (R5/T8, reservation, Gate POST allocation, later allocation) are treated as historical inputs. Their evidentiary immutability was not independently re-reviewed in this Gate 0 workstream.
- A closure cannot be made safe solely by a record containing path strings or one digest. Exact run/manifest binding, no-follow identity checks, terminal and lease proof, and later mutation detection need an implementation-specific review.
