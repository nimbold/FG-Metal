# STEP 11D.3-G0C

STORAGE WORKFLOW STABLE — RESUME STEP 11D.3

Verified 2026-10-07T17:20:14.078709+03:30. No graphics experiment, drawable test, experimental build, or commit was performed.

| Check | Final result |
| --- | --- |
| Synthetic storage suite | 181 tests, PASS, no skips |
| Build retention and existing prefix policy | 139 tests, PASS |
| Launcher paths/security | 17 tests, PASS; independent review PASS |
| Admission, evidence hashes, pre/post checks | 15 tests, PASS |
| Zero-copy consolidation and recovery | 10 tests, PASS |
| Free disk | 74.35 GiB |
| Scoped FG-Metal storage | 42.45 GiB, including conservative unlinked mounted-image allowance |
| Project cap | 45 GiB; unchanged, approximately 2.55 GiB margin |
| Builds | active 0; preserved 0; retained 0; pending retirement 0; failed/retiring 0; ambiguous 0 |
| Prefixes | preserved 2; active 0; uncertified 0 |
| Worktrees | 3 |
| Fresh cleanup phase | PASS |
| PrefixLease smoke | PASS; prefix counts 2 → 3 → 2; markers 0 → 1 → 0 |
| Controlled command | /usr/bin/true, return code 0, supervised by PrefixLease |
| Fresh final attestation and runner admission | PASS; no blockers |
| git diff --check | PASS |

The two hardlink blockers were retained evidence references, not disposable evidence directories. The prior implementation demanded 587,539,545 bytes of isolation copying because the canonical DLLs still shared their inode with one evidence alias each. A verified absolute reference at each evidence pathname preserves path and content while leaving the canonical inode as the sole regular-file link. Both canonical DLLs retain their original inode, SHA-256 and manifest binding. No copy budget was raised, and zero payload bytes were copied.

Only 24 certified redundant DLL paths across 14 closed inode groups were consolidated. Twelve separate evidence inode groups held redundant bytes (~3.37 GiB); two groups were the blocking canonical hardlinks. The operation retains all paths and bytes through canonical references, binds full metadata in a signed plan/receipt, verifies the canonical SHA before unlink, writes durable per-action intents, and supports rollback and interrupted-exchange recovery. Required R5 artifacts, source, logs, manifests, and preserved prefixes remain available. The mounted image was retained; its conservative allowance remains in the gate total. The result provides about 2.55 GiB margin without removing additional protected material to reach the optional 42 GiB target.

Wine launches require the exact certified executable and server paths, revalidate the executable identity and pinned runtime tree at launch, and reject PATH-selected substitutions. DYLD uses exact absolute path components; parent escapes, empty entries, unsafe symlink directories, and unpinned candidates fail closed. The exact historical alias is normalized to the certified candidate, approved unrelated entries retain order, and duplicate components are deduplicated. Candidate and runtime fallback content is revalidated before each launch. System search directories remain available for unrelated entries but cannot supply an unpinned MoltenVK fallback.

PRE admission checks enforce current free/scoped thresholds, fresh signed evidence, source hashes, build/prefix caps, and unambiguous inventories. POST cleanup recomputes storage, verifies cleanup, emits the signed receipt, and durably blocks admission when cleanup or Gate 0 thresholds fail. Stale inventory, cleanup, control, test-log, supplemental, and independent-review evidence is covered by rejection tests.

The real smoke acquired one lease, created one disposable prefix, observed its MATERIALIZED marker and registry, ran the controlled command, cleaned normally, released the marker, and proved exact baseline return. Failure, exception, timeout, and cleanup-failure precedence are covered synthetically. The final source/evidence hashes are bound by the attestation, and the authorized diagnostic runner was checked without starting an experiment.

## Hardlink identities

- Canonical: `/Users/nima/Library/Caches/FGMetal/artifacts/sha256/531c71f02bc85b19b9788b7b5525a904779e4b0371cd47a5690ac676d97daded/d3d11.dll`
- SHA-256: `531c71f02bc85b19b9788b7b5525a904779e4b0371cd47a5690ac676d97daded`
- Original device/inode: `16777230/92216249`; original links 2, final links 1.
- Retained evidence reference: `/Users/nima/.codex/worktrees/step11d-r/FG-Metal/experiments/dxvk_macos_step11dR/evidence/source-built-11c-control-30s/flipseq-internal/d3d11.dll`

- Canonical: `/Users/nima/Library/Caches/FGMetal/artifacts/sha256/69f248484ab1fc9f6156e9a2a3b4ff9659a3484c65027e091a7b439eb382344f/dxgi.dll`
- SHA-256: `69f248484ab1fc9f6156e9a2a3b4ff9659a3484c65027e091a7b439eb382344f`
- Original device/inode: `16777230/92216250`; original links 2, final links 1.
- Retained evidence reference: `/Users/nima/.codex/worktrees/step11d-r/FG-Metal/experiments/dxvk_macos_step11dR/evidence/source-built-11c-control-30s/flipseq-internal/dxgi.dll`

## Evidence

- [storage-inventory.json](/Users/nima/Documents/Code/FG-Metal/experiments/storage/storage-inventory.json)
- [storage-cleanup-receipt.json](/Users/nima/Documents/Code/FG-Metal/experiments/storage/storage-cleanup-receipt.json)
- [g0c-hardlink-audit.json](/Users/nima/Documents/Code/FG-Metal/experiments/storage/g0c-hardlink-audit.json)
- [g0c-evidence-consolidation-plan.json](/Users/nima/Documents/Code/FG-Metal/experiments/storage/g0c-evidence-consolidation-plan.json)
- [g0c-evidence-consolidation-receipt.json](/Users/nima/Documents/Code/FG-Metal/experiments/storage/g0c-evidence-consolidation-receipt.json)
- [build-retention-tests.log](/Users/nima/Documents/Code/FG-Metal/experiments/storage/build-retention-tests.log)
- [g0c-launcher-tests.log](/Users/nima/Documents/Code/FG-Metal/experiments/storage/g0c-launcher-tests.log)
- [g0c-checks-tests.log](/Users/nima/Documents/Code/FG-Metal/experiments/storage/g0c-checks-tests.log)
- [g0c-consolidation-tests.log](/Users/nima/Documents/Code/FG-Metal/experiments/storage/g0c-consolidation-tests.log)
- [g0c-launcher-independent-review.json](/Users/nima/Documents/Code/FG-Metal/experiments/storage/g0c-launcher-independent-review.json)
- [g0c-prefixlease-smoke.log](/Users/nima/Documents/Code/FG-Metal/experiments/storage/g0c-prefixlease-smoke.log)
- [storage-gate0-attestation.json](/Users/nima/Documents/Code/FG-Metal/experiments/storage/storage-gate0-attestation.json)
- [g0c-admission-result.json](/Users/nima/Documents/Code/FG-Metal/experiments/storage/g0c-admission-result.json)
- [Authenticated smoke report](/Users/nima/Documents/Code/FG-Metal/experiments/storage/smoke-runs/step11d3-storage-policy-smoke-20261007-1b9d0f22399c43e2/prefix-smoke-report.json)
- [Authenticated smoke cleanup receipt](/Users/nima/Documents/Code/FG-Metal/experiments/storage/smoke-runs/step11d3-storage-policy-smoke-20261007-1b9d0f22399c43e2/prefix-cleanup.json)

Remaining blockers: none.
