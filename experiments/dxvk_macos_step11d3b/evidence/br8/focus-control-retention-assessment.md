# BR8 focus-control raw-evidence retention assessment

## Decision

The two specified runs are eligible only for a **lossless, exact-path per-file cold archive** of their raw traces and logs, after a dedicated BR8 allowlist/archive guard is added or reviewed. They are not technically rejected: each `run-result.json` records `status=PASS`, `is_valid=true`, stable control binding, and Gate 0 pre/post PASS. Their treatment sequences are incomplete, however, so neither is the canonical retained focus-control result. The later run `step11d3b-br8-state-02-20261011-005208-590fa5da` is the attained replacement and must remain expanded.

The first attempt, `step11d3b-br8-state-02-20261010-205140-6eeb0bbd`, attained the inactive/nonkey/nonmain hold after Finder activation, but target activation at about +60 seconds did not restore those states before trace end. The second, `step11d3b-br8-state-02-20261010-205756-a9b95137`, was already inactive at its first sample, has no recorded active-to-inactive transition, and did not attain restoration; its AX result conflicts with target AppKit telemetry and window ordering. The replacement records active → inactive → active target notifications and telemetry. These distinctions are documented in [the first state assessment](state-02-independent-assessment.md), [the repeat assessment](inactive-raise-repeat-independent-assessment.md), and [the attained replacement assessment](inactive-frontmost-independent-assessment.md).

## Exact archive candidate

The only candidate IDs are:

- `runs/step11d3b-br8-state-02-20261010-205140-6eeb0bbd`
- `runs/step11d3b-br8-state-02-20261010-205756-a9b95137`

The proposed raw allowlist is the following ten files. Sizes are read-only `st_size` values from these two named run directories; no hash or archive was produced.

| Run | Relative file | Logical bytes |
| --- | --- | ---: |
| `...205140-6eeb0bbd` | `app-trace.jsonl` | 30,472,876 |
|  | `runtime.log` | 1,168,289 |
|  | `process-runner.log` | 528 |
|  | `host-traces/host-trace.2627.jsonl` | 55,868,424 |
|  | `host-traces/host-trace.2645.jsonl` | 1,823 |
| `...205756-a9b95137` | `app-trace.jsonl` | 30,772,498 |
|  | `runtime.log` | 1,161,948 |
|  | `process-runner.log` | 527 |
|  | `host-traces/host-trace.3197.jsonl` | 56,412,537 |
|  | `host-traces/host-trace.3215.jsonl` | 1,823 |

The candidate raw input totals 175,861,273 logical bytes (167.7 MiB). The two complete directories total 285,020,349 logical bytes (271.8 MiB); 109,159,076 bytes remain protected under this plan. Archive size, compression ratio, allocated bytes reclaimed, and APFS clone effects are **UNKNOWN**. Do not count the raw input total as net Gate 0 savings because the archive itself remains in the repository.

Keep every other file in both run directories expanded and byte-for-byte untouched: `run-result.json`, `run-manifest.json`, `execution-result.json`, `prefix-cleanup.json`, `prefix-state.json`, `process-context.json`, `trace-index.json`, `prefix-registry-diff.txt.gz`, and all three MoltenVK dylib copies under `mvk/` and `trace-loader/`. Keep the two storage admission manifests under `experiments/storage/manifests`, all run/hash/receipt/report/analysis/controller evidence, and the original run directories. The unchanged JSON and reports will still point at their original raw paths; the archive receipt must record each original relative path, member path, file hash, and size and provide exact restore instructions. Those direct links will not resolve while the raw files are archived.

Do not select `step11d3b-br8-state-02-20261011-005208-590fa5da`, the uncontrolled state-01 run `step11d3b-br8-state-01-20261010-202408-c3f551ea`, any other BR8 runs, or any accepted BR6/BR7 evidence. Keep the BR6/BR7 runs, reports, analyses, manifests, binaries, hashes, and receipts untouched. Keep D-transient evidence expanded as instructed.

## Existing tooling and discovery guard

The current [BR7 rejection archiver](archive_rejected_br7.py) cannot safely be reused unchanged. Its candidate IDs and validation contract are BR7-specific; its run validator requires a technical FAIL. Its plan requires root `visual-observations.json` and its raw selector requires screenshots, but neither BR8 candidate directory contains that JSON or screenshots. It therefore does not cover these technically passing, sequence-incomplete BR8 runs. The older [superseded-run script](../br7/archive_superseded.py) archives and removes whole selected directories, including JSON and binaries, and its selector does not include these state-02 IDs. Neither is an approved match for the required per-file plan.

Statically, `storage_inventory.py` scans the main checkout recursively without following symlinks. Its classifier treats paths under `/evidence/` as preserved evidence and other main-checkout paths as protected worktree data. It has no run-result semantic check for missing archived raw paths. A future archive under `evidence/br8` and per-file retirement under `runs/` will therefore change inventory contents/totals; the archive is still counted. `storage_policy._gate0_blockers()` compares the inventory file hash to the signed Gate 0 attestation and blocks when they differ. Before the next graphics run, use the normal Gate 0 refresh path to regenerate storage discovery and bind the new inventory in a fresh passing attestation. No inventory, Gate 0, or storage test was run for this assessment.

## Headroom

Using the supplied scoped total of 43.203 GiB against the 45 GiB cap gives 1.797 GiB nominal room. The supplied future demand estimate is 7 × 0.134 GiB + 0.600 GiB + 0.850 GiB = 2.388 GiB, a 0.591 GiB shortfall before retirement. The candidates contain at most 0.164 GiB of logical raw input, before subtracting the archive's allocated size; that upper bound cannot close the projected shortfall. The archive may still provide useful margin, but it is not sufficient headroom evidence by itself. Recompute from a fresh storage discovery after any future retirement.

## Evidence boundary

Read-only inspection was limited to the two candidates, the attained replacement, their retained assessment files, and static archive/storage-discovery source. Per-file logical sizes came from `lstat` on those named directories only. No archive, unlink, mutation, storage-inventory/Gate 0 command, graphics run, build, or test was executed. The supplied 43.203 GiB and workload estimates were treated as inputs, not refreshed measurements.
