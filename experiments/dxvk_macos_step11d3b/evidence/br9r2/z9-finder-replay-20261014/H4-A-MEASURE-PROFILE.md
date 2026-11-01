# H4-A — `measure_project_usage()` filesystem/performance profile

## Scope and method

Read-only profile of `experiments/storage/storage_policy.py::measure_project_usage()` in the current checkout. No product source was modified. A standalone temporary profiler at `/tmp/h4_profile_measure_usage.py` wrapped `os.walk`, `Path.stat`/`lstat`, `Path.parents`, and the parent-sequence iterator; its captured output is `/tmp/h4_profile_measure_usage.out`. It invoked the function once with the current signed attestation's 66 persistent-output bindings expanded to 132 normalized exclusions (each manifest and output directory). This exact-shape instrumented scan completed naturally; it was not a pristine benchmark. No second full scan was started.

The user-provided accepted run boundary is a prior call that ran for more than one hour, was interrupted with SIGINT, and showed the stack at the exclusion accounting expression near line 3177. The 493.8-second run below is our instrumented reproduction and must not be substituted for or treated as the duration of that accepted run.

## Full-call results

The profiled call completed in **493.824669 seconds**. It scanned 11 classified roots: the repository, two Git worktrees, and eight cache roots. Aggregates:

- 6,411 directories yielded by `os.walk`; 6,432 directory entries and 53,507 filename entries observed.
- 53,033 regular file paths counted; 451 symlinks were identified with `lstat` and were not counted as regular files. `os.walk` used its default non-following symlink-directory behavior.
- 51,888 distinct `(st_dev, st_ino)` keys in the allocation map; 50,826 inode keys remained included after exclusions. The 1,062-key difference occurred only in excluded paths. There were 1,145 additional regular-file paths beyond unique inode keys, consistent with hardlink aliases, which are inode-deduplicated.
- Logical bytes: 95,000,788,987. Allocated, inode-deduplicated bytes: 49,976,610,816; after exclusions: 46,748,024,832. `hardlinks_deduplicated=True`, `apfs_clone_awareness=False`, and no disappearance events.
- 6,400 `Path.stat` calls (0.01515 s wrapped time) and 53,506 `Path.lstat` calls (1.45933 s wrapped time). `os.walk` iterator/enumeration time was 1.17845 s total. Consumer bodies took 492.59599 s. These timings identify Python-side per-entry work, not filesystem enumeration or metadata syscalls, as the dominant cost.

### Per-root timings

Elapsed is the wrapped `os.walk` lifetime for each root. Counts are yielded directories / directory entries / filename entries.

| Root | Elapsed | Directories / dir entries / filenames |
|---|---:|---:|
| `/Users/nima/Documents/Code/FG-Metal` | 223.5127 s | 1,991 / 1,994 / 22,306 |
| `/Users/nima/Library/Caches/FGMetalStep11D-R` | 103.5447 s | 1,151 / 1,159 / 11,858 |
| `/Users/nima/Library/Caches/FGMetalStep11C1R` | 79.2480 s | 941 / 942 / 9,032 |
| `/Users/nima/Library/Caches/FGMetal` | 60.9445 s | 1,721 / 1,729 / 7,054 |
| `/Users/nima/.codex/worktrees/step11d-r/FG-Metal` | 19.5032 s | 462 / 465 / 2,312 |
| `/Users/nima/.codex/worktrees/step-11d/FG-Metal` | 6.3245 s | 78 / 81 / 832 |
| `/Users/nima/Library/Caches/FGMetalCleanup-20261005` | 0.5345 s | 57 / 56 / 75 |
| `/Users/nima/Library/Caches/FG-Metal-Step8D-20261002` | 0.1753 s | 7 / 6 / 36 |
| `/Users/nima/Library/Caches/lsfg-metal` | 0.01086 s | 1 / 0 / 2 |
| `/Users/nima/Library/Caches/FG-Metal-Step8C` | 0.000886 s | 1 / 0 / 0 |
| `/Users/nima/Library/Caches/FGMetalStep11D` | 0.000452 s | 1 / 0 / 0 |

### Hot directories and ATen separation

The largest consumer-body cost was:

`/Users/nima/Documents/Code/FG-Metal/models/local/rife-v4.26/venv/lib/python3.11/site-packages/torch/include/ATen/ops`

The full scan processed **7,235 direct regular-file names and zero direct directories** there. Its per-directory consumer body took **85.611742 s**, while the corresponding `os.walk` next/enumeration step took **0.021959 s**. A bounded direct-entry census separately measured `os.scandir` at **0.01989 s** for exactly 7,235 regular files, with no symlinks or subdirectories. The parent `ATen` directory itself had 119 regular files and 13 directories; its full-scan body was 1.717 s versus 0.00107 s for enumeration. This isolates ATen's long time to per-file accounting rather than an intrinsically slow directory read.

Other largest measured consumer bodies were Wine trees: `FGMetalStep11C1R/.../wine/include/wine/windows` (1,332 files, 11.3226 s), `FGMetalStep11D-R/.../wine/include/wine/windows` (1,332, 10.3221 s), then their `i386-windows` and `x86_64-windows` directories (1,094/1,042 files, 8.2–9.4 s). The `FGMetal` pristine Wine prefix `syswow64` and `system32` directories took 6.80 s and 6.58 s. Their `os.walk` next times were milliseconds or less.

## Exclusion predicate measurement

The expensive expression at the reported source line is `any(path == excluded or excluded in path.parents for excluded in normalized_exclusions)`. For a nonmatching file and 132 exclusions, this rebuilds and walks the file's ancestor sequence repeatedly.

Instrumentation recorded 6,956,940 accesses to `Path.parents`, 92,020,702 yielded ancestor items, and 6,955,944 iterator exhaustions. The total item-access count including iterator-end checks was 98,976,646. A 1,511-call timing sample of the underlying parent-item operation averaged 3.82 microseconds; extrapolation is roughly 378 s of ancestor construction/iteration. This is a sampled estimate, not an independently timed whole-call decomposition. The profiler's direct `Path.parents` property wrapper time was 6.969 s and does not include the later iterator work.

A bounded, uninstrumented A/B on 500 evenly spaced live paths from `ATen/ops`, using the same 132 exclusions, measured:

| Predicate | Elapsed | Per path |
|---|---:|---:|
| Current repeated `excluded in path.parents` expression | 1.3219 s | 2.6438 ms |
| One exclusion set plus one ancestor-chain membership pass | 0.012477 s | 0.02495 ms |

The indexed form was about **106× faster** on this sample. Its result matched the current predicate for all 500 live paths and for checks covering exact excluded paths, descendants of excluded paths, and nonmatching parents. This is direct evidence that exclusion ancestry checks dominate the hot directory and the full call.

## Roots, filesystem, symlinks, and contents

- `st_dev` for all 11 roots was 16777229. The mount inventory placed these paths on local `/dev/disk3s1` APFS under `/System/Volumes/Data`; no listed mount point was nested below a scanned root. The project-root mount list had no network or FUSE filesystem, and the mounted-image inventory listed no mounted backing image. A per-descendant `st_dev` histogram was not separately captured, so the no-descendant-transition statement is based on mount-point inventory and matching root devices, not exhaustive device sampling at every file.
- `os.walk` did not follow symlinked directories; `lstat` identified 451 symlink entries. The regular-file count excludes those symlinks. The profiler did not separately classify every non-regular `lstat` result as a socket/FIFO/device; that quantity is not available.
- Main `.git` was included in the repository-root traversal because there is no `.git` prune. The existing storage inventory reports 2,103 `.git` files, about 1,857,906,706 logical bytes and 1,863,680,000 allocated bytes. Its directories were not among the slowest consumer bodies. `measure_project_usage()` does not read file contents or hash files; it uses path metadata and allocated-block metadata.
- Repository experiments/evidence, including run and storage evidence, are part of the repository traversal. Their separate subtree elapsed/count was not captured in this profile. The scan also includes the local RIFE model virtual environment and Torch headers, cache runtime/build outputs and Wine prefixes. There is no general prune for `.git`, virtual environments, model assets, or build/evidence outputs; the classifier only filters paths with components matching its explicit other-project exclusions.
- The 11 classified roots are resolved and path-deduplicated before traversal; no identical root was traversed twice. The Wine/cache trees do contain repeated *content/layout* at distinct paths. Hardlinks are deduplicated by `(device,inode)`, but APFS clones/content-identical files are not (`apfs_clone_awareness=False`).
- No per-file subprocess or hash behavior occurred. Root classification launched one `git worktree list --porcelain` subprocess; that is a single discovery operation, not a per-entry operation.

## Assessment and smallest safe fix

The accepted >1-hour interruption plus the line-3177 stack is consistent with an **extremely slow CPU-bound traversal**, not a filesystem hang: an exact-shape instrumented reproduction completed naturally, spent almost all elapsed time in the Python consumer, and the hottest directory enumerated in about 22 ms but spent 85.6 s in per-file accounting. The measured predicate repeatedly materializes/walks `Path.parents` once per exclusion per path. It explains the hot directory and measured runtime. The prior run's >1-hour duration remains the accepted-run observation; this instrumented reproduction does not establish that all invocations take the same time.

Smallest semantics-preserving change: construct `excluded_set = set(normalized_exclusions)` once, then test exact path membership and walk `path.parents` once, checking each ancestor against that set. This retains the current rule (exclude a path equal to an exclusion or beneath an excluded directory) without weakening accounting. The live A/B showed equivalence for sampled paths and boundary cases. No product change was made in this profile.
