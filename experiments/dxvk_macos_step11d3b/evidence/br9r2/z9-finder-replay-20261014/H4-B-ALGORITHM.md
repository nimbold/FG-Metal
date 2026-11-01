# H4-B — `measure_project_usage()` algorithm review

**Scope:** Read-only source review of `experiments/storage/storage_policy.py::measure_project_usage()`, its root discovery and admission helpers, and its storage Gate 0 / runner call sites. No product code was changed and no tests or live storage measurement were run.

## Finding

The byte total is a unique-inode sum of `st_blocks * 512` for regular files reachable from the discovered roots. `(st_dev, st_ino)` correctly prevents counting a hard-linked inode twice, including when its links appear under different roots. `logical_bytes` and `file_count`, however, count visited pathnames, so hardlinks and overlapping roots can appear more than once in those two diagnostic fields. The result explicitly sets `apfs_clone_awareness` to `False`.

The implementation includes the repository root without pruning `.git`; `.git` object packs, loose objects, refs, and other regular files are therefore part of the walk. It also includes linked Git worktrees, matching FG-Metal cache roots, selected named temporary roots, and matching MoltenVK Xcode DerivedData roots. `_is_other_project_path()` excludes paths with a component containing `dxmt`, `framegen`, or `step10b6_1`; no `.git` exclusion exists.

## Accounting and edge cases

- **Hardlinks:** `allocated_by_inode` is keyed by device and inode, and the first regular-file stat supplies the allocation. Exclusion accounting marks an inode included if any encountered link lies outside an excluded path. This correctly keeps an inode linked both inside and outside an excluded lease/output tree in the non-excluded total. `logical_bytes` remains pathname-based.
- **Symlinks and devices:** The root is `lstat()`ed, symlink roots are rejected, and a discovered root's `(dev, ino)` is rechecked when available. `os.walk(..., followlinks=False)` avoids ordinary symlink-directory traversal; child directories on a different `st_dev` from the root are pruned. Regular files are `lstat()`ed and symlinks/special files are not counted. The walk is path-based rather than descriptor-anchored, so these checks do not close check/use races. A directory swapped for a symlink between the checks and descent can redirect traversal; same-device mount crossings are not distinguished by the `st_dev` comparison.
- **APFS clones and mounted images:** Distinct clone inodes are each charged their reported blocks, with no shared-extent credit. This may overstate attributable allocation, but does not give admission a sharing discount. Files absent from the namespace, including unlinked mounted-image backing, cannot be discovered by this walk; `conservative_project_usage_bytes()` adds the inventory's unlinked-mount upper bound and takes the maximum with the attested scoped value.
- **Overlapping roots:** `_classified_project_roots()` removes roots with the same `Path.resolve(strict=False)` string, not ancestor/descendant roots. Nested roots can therefore be walked more than once. The allocation sum remains inode-deduplicated, while logical bytes and file count are multiplied by the number of traversals. Distinct roots retain separate identity and optional-disappearance handling.
- **Root and temporary scope:** The walk is intentionally bounded to configured root classes, not every cache or temporary file on the machine. `/private/tmp` candidates matching the FG-Metal/step prefixes are included except names containing `-test-`, beginning `test-`, or containing `synthetic`. These are root-selection rules; they do not justify dropping `.git` or other bytes inside an admitted root.

## Correctness risks to preserve in follow-up

1. **Descendant I/O errors can undercount silently.** `os.walk()` has no `onerror` callback, so directory-enumeration errors use its default ignore behavior. In addition, failures from child `stat()` and file `lstat()` are caught and continued over. This differs from the fail-closed handling of mandatory root errors and can produce a plausible partial total for unreadable, disappearing, or otherwise failing descendants. Optional root disappearance before its initial stat is instead recorded explicitly, as intended.
2. **Worktree discovery can undercount silently.** `git worktree list --porcelain` is invoked with `check=False` and stderr discarded; its return code is not checked. A failed listing can leave the repository as the only discovered Git root, omitting live worktrees. Candidate filtering also uses `Path.is_dir()` before adding roots, which should not become a silent substitute for reporting discovery errors.
3. **The walk is not an atomic filesystem snapshot.** Files and directories may change during traversal, and `(dev, ino)` may be reused across a delete/recreate race. Gate 0 performs a second measurement and requires matching observed totals, exclusions, bindings, and bounded free-space drift; that detects many changes but does not make the traversal itself snapshot-consistent.
4. **Missing allocation metadata defaults to zero.** `getattr(info, "st_blocks", 0)` turns an unavailable `st_blocks` field into a zero-byte charge. On the target macOS filesystem the field is expected, but a portable caller or unusual filesystem should fail closed rather than silently claim exact allocation when it is absent.

The existing targeted tests cover hardlink preservation through an excluded tree, signed-output exclusion binding, and root-level disappearance/symlink/identity/permission failures. The inspected tests do not exercise descendant `os.walk` error propagation, overlapping-root diagnostic multiplicity, APFS clone behavior, or absent `st_blocks`. No tests were run for this review.

## Caller and admission contract

`measure_project_usage()` is consumed by `_gate0_blockers()` for live scope and historical closure, Gate 0 finalization (including a second verification scan), experiment manifest admission, PrefixLease and NativeRunLease admission/rechecks, and before/after cleanup receipts. `runner_b.py::gate0_snapshot()` also reports its live result. The scanner is therefore part of both admission and the later change-detection record, not just a display estimate.

The normal projected admission adds the complete declared peak to current scoped usage and subtracts it from free space. Current policy constants retain a **50 GiB minimum free-space** requirement and a **55 GiB scoped-project ceiling** (with a 52 GiB warning); Gate 0 independently caps current scoped use at 55 GiB. `_gate0_blockers()` preserves historical closure by comparing signed historical output bindings and requiring observed non-lease bytes to equal the attested baseline. Exact measurement fixes must keep these comparisons and thresholds intact. Inventory-only unlinked backing is added conservatively; APFS sharing is not credited.

## Exact-accounting optimizations

**Yes, small exact optimizations exist without narrowing the measured scope or changing results:**

- Replace `included_inodes` with an `included` bit stored alongside each inode's allocated-byte value. OR the bit when any hardlink path is outside the exact normalized exclusions, then sum the same records for the total and non-excluded total. This preserves inode deduplication and the “any included link keeps the inode” rule while removing a second inode-sized set.
- Precompute a set of normalized excluded paths and test each candidate path's ancestors against that set. This preserves the current path/exclusion rule while avoiding a full scan of every exclusion for every file.

Neither optimization prunes a root, skips `.git`, excludes legitimate files, changes inode identity, or applies APFS sharing credit. A broader nested-root scan-elision could reduce traversal work, but it must retain each root's identity/disappearance checks and preserve current pathname-based logical/file-count semantics; do not simply skip nested roots. Any optimization also needs to preserve the historical closure and 50/55 GiB policy unchanged.

## Source references

- Root selection and protected-path filter: `experiments/storage/storage_policy.py:408-474`.
- Inode, exclusion, and traversal behavior: `experiments/storage/storage_policy.py:3092-3192`.
- Conservative mounted-image/attested bound and projected thresholds: `experiments/storage/storage_policy.py:315-370`.
- Historical output and non-lease closure: `experiments/storage/storage_policy.py:2955-2994`.
- Gate 0 two-pass measurement: `experiments/storage/storage_gate0.py:122-180`.
- Hardlink and signed-output accounting tests: `experiments/storage/test_storage_policy.py:319-339,424-470`.
- Root disappearance and failure tests: `experiments/storage/test_storage_recovery.py:57-217`.
